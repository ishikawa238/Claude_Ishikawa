"""
テクニカル指標計算モジュール

計算対象:
- MA5 / MA25 / MA75 / MA200（およびそれぞれ5日前の値）
- 20日平均出来高
- 20日高値・52週高値・52週安値
- 5日騰落率・20日騰落率
- RSI14（Wilder平滑化）
- ATR14
- 20日ボラティリティ（年率換算）
- 推計売買代金・推計時価総額

設計方針:
- pandas を使わず標準ライブラリ + 最小限の numpy のみで実装する
- price_history から直近756日分を読み込んで計算する
- 計算に必要なデータが不足する場合はフィールドを None にする
"""

import math
from datetime import date
from typing import Optional

from src.data_store import fetch_all, get_connection
from src.logger import get_logger

logger = get_logger(__name__)

# 計算に必要な最低日数
_MIN_DAYS_FOR_MA200 = 200
_MIN_DAYS_FOR_RSI = 15      # 14日分 + 初期化に1日
_MIN_DAYS_FOR_ATR = 15
_MIN_DAYS_FOR_VOLATILITY = 21

# 傾き判定に使う「N日前」
_SLOPE_LOOKBACK = 5


# ============================================================
# メイン計算関数
# ============================================================

def calculate_snapshot(ticker: str, target_date: date, prices: list[dict]) -> dict:
    """
    1銘柄・1日分のテクニカルスナップショットを計算する。

    引数:
        ticker:      銘柄コード
        target_date: 計算対象日
        prices:      直近756日分の価格データ（日付昇順）
                     各要素: {"date": "YYYY-MM-DD", "adj_close": float, "volume": float, ...}

    返り値: technical_snapshot テーブルへの INSERT 用辞書
    """
    # 対象日以前のデータのみ使用（昇順ソート済み前提）
    target_str = target_date.strftime("%Y-%m-%d")
    hist = [p for p in prices if p["date"] <= target_str and p["adj_close"]]

    if not hist:
        return _empty_snapshot(ticker, target_date)

    closes = [float(p["adj_close"]) for p in hist]
    highs  = [float(p["high"]) if p.get("high") else float(p["adj_close"]) for p in hist]
    lows   = [float(p["low"])  if p.get("low")  else float(p["adj_close"]) for p in hist]
    vols   = [float(p["volume"]) if p.get("volume") else 0.0 for p in hist]

    n = len(closes)
    today_close = closes[-1]
    today_vol   = vols[-1]

    # 当日の発行済株式数（universe_master から別途渡す設計だが、ここでは snapshot に含めない）
    # est_market_cap は呼び出し元で shares_outstanding をかけて計算する

    result = {
        "ticker": ticker,
        "date":   target_str,
        # 移動平均
        "ma5":          _sma(closes, 5),
        "ma25":         _sma(closes, 25),
        "ma75":         _sma(closes, 75),
        "ma200":        _sma(closes, 200),
        "ma5_5d_ago":   _sma(closes[:-_SLOPE_LOOKBACK], 5)   if n > _SLOPE_LOOKBACK + 5   else None,
        "ma25_5d_ago":  _sma(closes[:-_SLOPE_LOOKBACK], 25)  if n > _SLOPE_LOOKBACK + 25  else None,
        "ma200_5d_ago": _sma(closes[:-_SLOPE_LOOKBACK], 200) if n > _SLOPE_LOOKBACK + 200 else None,
        # 出来高・高値
        "vol_20d_avg":  _mean(vols[-20:])   if n >= 20 else None,
        "high_20d":     max(closes[-20:])   if n >= 20 else None,
        "high_52w":     max(closes[-252:])  if n >= 252 else max(closes),
        "low_52w":      min(closes[-252:])  if n >= 252 else min(closes),
        # 騰落率
        "return_5d":    _return_pct(closes, 5),
        "return_20d":   _return_pct(closes, 20),
        # オシレーター
        "rsi14":        _rsi(closes, 14),
        "atr14":        _atr(closes, highs, lows, 14),
        "volatility_20d": _volatility(closes, 20),
        # 推計値
        "est_turnover":   today_close * today_vol if today_vol else None,
        "est_market_cap": None,  # 呼び出し元で shares_outstanding × today_close を設定
    }
    return result


def calculate_index_mas(index_code: str, prices: list[dict]) -> list[dict]:
    """
    インデックスの MA を計算して index_snapshot 更新用データを返す。

    引数:
        index_code: N225 / TOPIX / SP500 / NASDAQ
        prices: 直近756日分の価格データ（日付昇順）

    返り値: index_snapshot の UPSERT 用辞書リスト（全日付分）
    """
    rows = []
    for i, p in enumerate(prices):
        closes_up_to = [float(q["close"]) for q in prices[:i + 1] if q.get("close")]
        n = len(closes_up_to)

        ma25   = _sma(closes_up_to, 25)
        ma75   = _sma(closes_up_to, 75)
        ma200  = _sma(closes_up_to, 200)
        ma25_5d_ago = (
            _sma(closes_up_to[:-_SLOPE_LOOKBACK], 25)
            if n > _SLOPE_LOOKBACK + 25 else None
        )

        rows.append({
            "index_code":   index_code,
            "date":         p["date"],
            "close":        float(p["close"]),
            "ma25":         ma25,
            "ma75":         ma75,
            "ma200":        ma200,
            "ma25_5d_ago":  ma25_5d_ago,
        })
    return rows


# ============================================================
# DB から価格データを読み込んで一括計算する
# ============================================================

def calculate_and_save_snapshots(
    tickers_with_market: list[tuple[str, str]],
    target_date: date,
    shares_map: dict[str, Optional[float]] = None,
) -> int:
    """
    指定日付の全銘柄のテクニカルスナップショットを計算して DB に保存する。

    引数:
        tickers_with_market: [(ticker, market), ...]
        target_date: 計算対象日
        shares_map: {ticker: shares_outstanding} (時価総額計算用、省略可)

    返り値: 保存した件数
    """
    from src.data_store import upsert

    if shares_map is None:
        shares_map = {}

    target_str = target_date.strftime("%Y-%m-%d")
    # 756日前から対象日まで読み込む
    from datetime import timedelta
    from_str = (target_date - timedelta(days=756)).strftime("%Y-%m-%d")

    saved = 0
    batch_rows = []

    for ticker, market in tickers_with_market:
        prices = _load_prices(ticker, from_str, target_str)
        if not prices:
            continue

        snapshot = calculate_snapshot(ticker, target_date, prices)

        # 時価総額の補完
        shares = shares_map.get(ticker)
        if shares and snapshot.get("est_turnover") is not None:
            today_close = float(prices[-1]["adj_close"]) if prices else None
            if today_close:
                snapshot["est_market_cap"] = today_close * shares

        batch_rows.append(snapshot)

        # 500件ごとに一括保存（メモリ効率）
        if len(batch_rows) >= 500:
            with get_connection() as conn:
                upsert(conn, "technical_snapshot", batch_rows)
            saved += len(batch_rows)
            batch_rows = []

    if batch_rows:
        with get_connection() as conn:
            upsert(conn, "technical_snapshot", batch_rows)
        saved += len(batch_rows)

    logger.info(f"テクニカル計算完了: {saved} 件保存 (対象日: {target_str})")
    return saved


def calculate_and_save_index_mas(index_code: str, target_date: date) -> None:
    """
    インデックスの MA を計算して index_snapshot を更新する。
    """
    from datetime import timedelta
    from src.data_store import upsert

    from_str = (target_date - timedelta(days=756)).strftime("%Y-%m-%d")
    target_str = target_date.strftime("%Y-%m-%d")

    with get_connection() as conn:
        rows = fetch_all(
            conn,
            "SELECT date, close FROM index_snapshot "
            "WHERE index_code = ? AND date BETWEEN ? AND ? ORDER BY date",
            (index_code, from_str, target_str),
        )

    if not rows:
        logger.warning(f"インデックスデータなし: {index_code}")
        return

    prices = [{"date": r["date"], "close": r["close"]} for r in rows]
    updated_rows = calculate_index_mas(index_code, prices)

    with get_connection() as conn:
        upsert(conn, "index_snapshot", updated_rows)

    logger.info(f"インデックスMA計算完了: {index_code} {len(updated_rows)} 件")


# ============================================================
# 指標計算ヘルパー（純粋関数）
# ============================================================

def _sma(closes: list[float], period: int) -> Optional[float]:
    """単純移動平均。データが period 日分に満たない場合は None。"""
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def _mean(values: list[float]) -> Optional[float]:
    """平均値。空リストの場合は None。"""
    if not values:
        return None
    return sum(values) / len(values)


def _return_pct(closes: list[float], period: int) -> Optional[float]:
    """N日騰落率（%）。データ不足の場合は None。"""
    if len(closes) <= period:
        return None
    prev = closes[-(period + 1)]
    if prev == 0:
        return None
    return (closes[-1] - prev) / prev * 100


def _rsi(closes: list[float], period: int = 14) -> Optional[float]:
    """
    Wilder 平滑化方式の RSI。
    計算に period + 1 日分以上のデータが必要。
    """
    if len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    # 初期値: 最初の period 日分の単純平均
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder 平滑化
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    period: int = 14,
) -> Optional[float]:
    """
    Average True Range（14日）。
    True Range = max(高値-安値, |高値-前日終値|, |安値-前日終値|)
    """
    if len(closes) < period + 1:
        return None

    trs = []
    for i in range(1, len(closes)):
        h, l, prev_c = highs[i], lows[i], closes[i - 1]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)

    if len(trs) < period:
        return None

    # Wilder 平滑化
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period

    return atr


def _volatility(closes: list[float], period: int = 20) -> Optional[float]:
    """
    終値対数収益率の標準偏差（年率換算・%）。
    年率換算係数: sqrt(252)
    """
    if len(closes) < period + 1:
        return None

    recent = closes[-(period + 1):]
    log_returns = []
    for i in range(1, len(recent)):
        if recent[i - 1] > 0 and recent[i] > 0:
            log_returns.append(math.log(recent[i] / recent[i - 1]))

    if len(log_returns) < 2:
        return None

    mean_lr = sum(log_returns) / len(log_returns)
    variance = sum((r - mean_lr) ** 2 for r in log_returns) / (len(log_returns) - 1)
    daily_std = math.sqrt(variance)
    return daily_std * math.sqrt(252) * 100  # 年率・%


def _empty_snapshot(ticker: str, target_date: date) -> dict:
    """データ不足時の全フィールド None のスナップショット。"""
    return {
        "ticker": ticker,
        "date":   target_date.strftime("%Y-%m-%d"),
        "ma5": None, "ma25": None, "ma75": None, "ma200": None,
        "ma5_5d_ago": None, "ma25_5d_ago": None, "ma200_5d_ago": None,
        "vol_20d_avg": None, "high_20d": None, "high_52w": None, "low_52w": None,
        "return_5d": None, "return_20d": None,
        "rsi14": None, "atr14": None, "volatility_20d": None,
        "est_turnover": None, "est_market_cap": None,
    }


# ============================================================
# DB からの価格読み込み
# ============================================================

def _load_prices(ticker: str, from_str: str, to_str: str) -> list[dict]:
    """price_history から指定期間のデータを昇順で取得する。"""
    with get_connection() as conn:
        rows = fetch_all(
            conn,
            "SELECT date, open, high, low, adj_close, volume "
            "FROM price_history "
            "WHERE ticker = ? AND date BETWEEN ? AND ? AND is_holiday = 0 "
            "ORDER BY date ASC",
            (ticker, from_str, to_str),
        )
    return [dict(r) for r in rows]
