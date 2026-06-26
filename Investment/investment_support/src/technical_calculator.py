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
- 週足MA25（長期トレンド確認用）
- パラボリックSAR（トレンド転換検出用）
- サポート/レジスタンスレベル（反発ポイント検出用）

設計方針:
- pandas を使わず標準ライブラリ + 最小限の numpy のみで実装する
- price_history から直近756日分を読み込んで計算する
- 計算に必要なデータが不足する場合はフィールドを None にする
"""

import math
from collections import defaultdict
from datetime import date
from typing import Optional

from src.config import CONFIG
from src.data_store import fetch_all, get_connection
from src.logger import get_logger

logger = get_logger(__name__)

# 計算に必要な最低日数
_MIN_DAYS_FOR_MA200 = 200
_MIN_DAYS_FOR_RSI = 15      # 14日分 + 初期化に1日
_MIN_DAYS_FOR_ATR = 15
_MIN_DAYS_FOR_VOLATILITY = 21
_MIN_WEEKS_FOR_WEEKLY_MA25 = 25
_MIN_DAYS_FOR_SUPPORT = 30   # サポート検出に必要な最低日数

# 傾き判定に使う「N日前」
_SLOPE_LOOKBACK = 5
_WEEKLY_SLOPE_LOOKBACK = 5   # 週足の傾き判定に使う「N週前」

# パラボリックSAR パラメータ（config から取得、デフォルト値あり）
_SIG = CONFIG.get("signal", {})
_SAR_AF_START = float(_SIG.get("pattern_c_sar_af_start", 0.02))
_SAR_AF_STEP = float(_SIG.get("pattern_c_sar_af_step", 0.02))
_SAR_AF_MAX = float(_SIG.get("pattern_c_sar_af_max", 0.20))
_SUPPORT_LOOKBACK = int(_SIG.get("pattern_c_support_lookback_days", 120))


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
        # EMA / MACD（スイング戦略）
        "ema25":        _ema(closes, 25),
        "ema75":        _ema(closes, 75),
        **_macd_fields(closes),
        # 推計値
        "est_turnover":   today_close * today_vol if today_vol else None,
        "est_market_cap": None,  # 呼び出し元で shares_outstanding × today_close を設定
    }

    # --- 週足MA25（Pattern C: 長期トレンド確認） ---
    weekly_candles = _aggregate_weekly(hist)
    weekly_closes = [w["close"] for w in weekly_candles]
    result["weekly_ma25"] = _sma(weekly_closes, 25)
    if len(weekly_closes) > _WEEKLY_SLOPE_LOOKBACK + 25:
        result["weekly_ma25_5w_ago"] = _sma(
            weekly_closes[:-_WEEKLY_SLOPE_LOOKBACK], 25
        )
    else:
        result["weekly_ma25_5w_ago"] = None

    # --- パラボリックSAR（Pattern C: トレンド転換検出） ---
    sar_val, sar_dir = _parabolic_sar(
        highs, lows, _SAR_AF_START, _SAR_AF_STEP, _SAR_AF_MAX
    )
    result["parabolic_sar"] = sar_val
    result["sar_direction"] = sar_dir

    # --- サポート/レジスタンス（Pattern C: 反発ポイント検出） ---
    sup, res = _detect_support_resistance(closes, highs, lows, _SUPPORT_LOOKBACK)
    result["support_level"] = sup
    result["resistance_level"] = res

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


def _aggregate_weekly(daily_prices: list[dict]) -> list[dict]:
    """
    日足OHLCVデータを週足に集約する。

    引数:
        daily_prices: 日付昇順の価格データリスト
                      各要素: {"date": "YYYY-MM-DD", "open": float, "high": float,
                               "low": float, "adj_close": float, "volume": float}

    返り値: 週足キャンドルリスト（日付昇順）
            各要素: {"week": (year, week_num), "open": float, "high": float,
                     "low": float, "close": float, "volume": float}
    """
    if not daily_prices:
        return []

    weeks: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for p in daily_prices:
        d = date.fromisoformat(p["date"]) if isinstance(p["date"], str) else p["date"]
        iso = d.isocalendar()
        weeks[(iso[0], iso[1])].append(p)

    result = []
    for week_key in sorted(weeks.keys()):
        days = weeks[week_key]
        o = float(days[0].get("open") or days[0]["adj_close"])
        h = max(float(d.get("high") or d["adj_close"]) for d in days)
        l = min(float(d.get("low") or d["adj_close"]) for d in days)
        c = float(days[-1]["adj_close"])
        v = sum(float(d.get("volume") or 0) for d in days)
        result.append({
            "week": week_key,
            "open": o, "high": h, "low": l, "close": c, "volume": v,
        })
    return result


def _parabolic_sar(
    highs: list[float],
    lows: list[float],
    af_start: float = 0.02,
    af_step: float = 0.02,
    af_max: float = 0.20,
) -> tuple[Optional[float], Optional[str]]:
    """
    Welles Wilder のパラボリックSAR を計算する。

    返り値: (sar_value, direction)
        direction = "up"  → SARがローソク足の下（上昇トレンド）
        direction = "down" → SARがローソク足の上（下降トレンド）
        データ不足時は (None, None)
    """
    n = len(highs)
    if n < 2:
        return None, None

    # 初期化: 最初の2本で方向を判定
    if highs[1] >= highs[0]:
        is_uptrend = True
        sar = lows[0]
        ep = highs[1]  # Extreme Point
    else:
        is_uptrend = False
        sar = highs[0]
        ep = lows[1]

    af = af_start

    for i in range(2, n):
        # SAR を更新
        prev_sar = sar
        sar = prev_sar + af * (ep - prev_sar)

        if is_uptrend:
            # SARは直近2本の安値より下に制限
            sar = min(sar, lows[i - 1], lows[i - 2])

            # トレンド転換判定
            if lows[i] < sar:
                is_uptrend = False
                sar = ep  # EPをSARとして使用
                ep = lows[i]
                af = af_start
            else:
                if highs[i] > ep:
                    ep = highs[i]
                    af = min(af + af_step, af_max)
        else:
            # SARは直近2本の高値より上に制限
            sar = max(sar, highs[i - 1], highs[i - 2])

            # トレンド転換判定
            if highs[i] > sar:
                is_uptrend = True
                sar = ep
                ep = highs[i]
                af = af_start
            else:
                if lows[i] < ep:
                    ep = lows[i]
                    af = min(af + af_step, af_max)

    direction = "up" if is_uptrend else "down"
    return round(sar, 4), direction


def _detect_support_resistance(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    lookback: int = 120,
) -> tuple[Optional[float], Optional[float]]:
    """
    ローカル極小値・極大値のクラスタリングでサポート/レジスタンスを検出する。

    引数:
        closes: 終値リスト（日付昇順）
        highs: 高値リスト
        lows: 安値リスト
        lookback: 検出に使用する日数

    返り値: (support_level, resistance_level)
        データ不足時は (None, None)
    """
    n = len(lows)
    if n < _MIN_DAYS_FOR_SUPPORT:
        return None, None

    # lookback 日分に絞る
    recent_lows = lows[-lookback:] if n > lookback else lows
    recent_highs = highs[-lookback:] if n > lookback else highs
    current_close = closes[-1]

    # ローカル極小値（安値が前後より低い）を検出
    local_mins: list[float] = []
    for i in range(1, len(recent_lows) - 1):
        if recent_lows[i] <= recent_lows[i - 1] and recent_lows[i] <= recent_lows[i + 1]:
            local_mins.append(recent_lows[i])

    # ローカル極大値（高値が前後より高い）を検出
    local_maxs: list[float] = []
    for i in range(1, len(recent_highs) - 1):
        if recent_highs[i] >= recent_highs[i - 1] and recent_highs[i] >= recent_highs[i + 1]:
            local_maxs.append(recent_highs[i])

    support = _find_strongest_cluster(local_mins, current_close, is_support=True)
    resistance = _find_strongest_cluster(local_maxs, current_close, is_support=False)

    return support, resistance


def _find_strongest_cluster(
    levels: list[float],
    current_price: float,
    is_support: bool,
    tolerance_pct: float = 0.02,
) -> Optional[float]:
    """
    価格レベルをクラスタリングし、最も頻出するクラスタの平均を返す。

    サポートの場合は現在価格より下のクラスタのみ、
    レジスタンスの場合は現在価格より上のクラスタのみを対象とする。
    """
    if not levels:
        return None

    # 現在価格に対するフィルタ
    if is_support:
        filtered = [lv for lv in levels if lv < current_price]
    else:
        filtered = [lv for lv in levels if lv > current_price]

    if not filtered:
        return None

    # 価格でソートしてクラスタリング
    filtered.sort()
    clusters: list[list[float]] = [[filtered[0]]]
    for lv in filtered[1:]:
        # 直前のクラスタの平均と比較
        cluster_avg = sum(clusters[-1]) / len(clusters[-1])
        if abs(lv - cluster_avg) / cluster_avg <= tolerance_pct:
            clusters[-1].append(lv)
        else:
            clusters.append([lv])

    # 最も反発回数が多いクラスタを選択
    best = max(clusters, key=len)
    if len(best) < 2:
        # 1回しか反発していない場合は信頼性が低い
        return None

    return round(sum(best) / len(best), 4)


def _ema(closes: list[float], period: int) -> Optional[float]:
    series = _ema_series(closes, period)
    return series[-1] if series else None


def _ema_series(closes: list[float], period: int) -> list[Optional[float]]:
    if len(closes) < period:
        return []
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    series = [None] * (period - 1) + [ema]
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
        series.append(ema)
    return series


def _macd_fields(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> dict:
    """MACD の当日値と前日値を返す。"""
    empty = {
        "macd_diff": None, "macd_dea": None, "macd_hist": None,
        "macd_diff_prev": None, "macd_dea_prev": None, "macd_hist_prev": None,
    }
    if len(closes) < slow + signal:
        return empty

    fast_series = _ema_series(closes, fast)
    slow_series = _ema_series(closes, slow)
    diff_series: list[Optional[float]] = []
    for f, s in zip(fast_series, slow_series):
        if f is None or s is None:
            diff_series.append(None)
        else:
            diff_series.append(f - s)

    valid_diffs = [d for d in diff_series if d is not None]
    if len(valid_diffs) < signal:
        return empty

    dea_tail = _ema_series(valid_diffs, signal)
    if not dea_tail:
        return empty

    diff = diff_series[-1]
    dea = dea_tail[-1]
    if diff is None or dea is None:
        return empty
    hist = diff - dea

    diff_prev = diff_series[-2] if len(diff_series) >= 2 else None
    dea_prev = dea_tail[-2] if len(dea_tail) >= 2 else None
    hist_prev = (
        diff_prev - dea_prev
        if diff_prev is not None and dea_prev is not None
        else None
    )

    return {
        "macd_diff": diff,
        "macd_dea": dea,
        "macd_hist": hist,
        "macd_diff_prev": diff_prev,
        "macd_dea_prev": dea_prev,
        "macd_hist_prev": hist_prev,
    }


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
        "ema25": None, "ema75": None,
        "macd_diff": None, "macd_dea": None, "macd_hist": None,
        "macd_diff_prev": None, "macd_dea_prev": None, "macd_hist_prev": None,
        "est_turnover": None, "est_market_cap": None,
        "weekly_ma25": None, "weekly_ma25_5w_ago": None,
        "parabolic_sar": None, "sar_direction": None,
        "support_level": None, "resistance_level": None,
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
