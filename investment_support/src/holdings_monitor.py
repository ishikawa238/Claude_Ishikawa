"""
保有銘柄監視モジュール

役割:
- holdings.csv を読み込んで holdings テーブルに同期する
- 各保有銘柄の当日終値・損益率を計算する
- 損切りライン到達 / 接近 / 売り検討 / 継続保有 を判定する
- 判定結果を通知モジュールへ渡す
"""

import csv
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from src.config import CONFIG
from src.data_store import fetch_all, fetch_one, get_connection, upsert
from src.logger import get_logger

logger = get_logger(__name__)

_PROJECT_ROOT    = Path(__file__).resolve().parent.parent
_HOLDINGS_CSV    = _PROJECT_ROOT / "holdings.csv"
_DEFAULT_SL_PCT  = float(CONFIG["capital"]["default_stop_loss_pct"]) / 100  # 0.08

# 損切りライン接近の警告閾値（損切りラインまで残り %）
_SL_NEAR_PCT = 2.0


# ============================================================
# CSV 読み込みと DB 同期
# ============================================================

def sync_holdings_from_csv() -> int:
    """
    holdings.csv を読み込んで holdings テーブルを更新する。
    CSV に存在しない行は変更しない（手動決済の記録を保持するため）。
    返り値: 同期件数
    """
    if not _HOLDINGS_CSV.exists():
        logger.warning(f"holdings.csv が存在しません: {_HOLDINGS_CSV}")
        logger.warning("保有銘柄が0件として処理を続行します。")
        return 0

    rows = []
    with open(_HOLDINGS_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for line in reader:
            ticker = line.get("ticker", "").strip()
            if not ticker:
                continue
            entry_price = _to_float(line.get("entry_price"))
            if not entry_price:
                logger.warning(f"entry_price が不正のためスキップ: {ticker}")
                continue

            stop_loss_price = _to_float(line.get("stop_loss_price"))
            if stop_loss_price is None:
                stop_loss_price = round(entry_price * (1 - _DEFAULT_SL_PCT), 2)

            rows.append({
                "ticker":          ticker,
                "name":            line.get("name", "").strip() or None,
                "market":          (line.get("market") or "").strip().upper(),
                "entry_price":     entry_price,
                "entry_date":      (line.get("entry_date") or "").strip() or None,
                "quantity":        _to_float(line.get("quantity")),
                "stop_loss_price": stop_loss_price,
                "memo":            line.get("memo", "").strip() or None,
                "is_closed":       0,
            })

    if rows:
        with get_connection() as conn:
            upsert(conn, "holdings", rows)

    logger.info(f"保有銘柄同期完了: {len(rows)} 件")
    return len(rows)


# ============================================================
# 保有銘柄の判定
# ============================================================

def evaluate_holdings(target_date: date) -> list[dict]:
    """
    保有銘柄全件を評価して判定結果リストを返す。
    未決済（is_closed=0）の保有銘柄のみ対象。

    返り値リストの各要素:
    {
        ticker, name, market,
        entry_price, entry_date, stop_loss_price, quantity, memo,
        current_price, pnl_pct, pnl_amount,
        label,           # "forced_stop" / "sl_near" / "sell_watch" / "hold"
        reasons: list[str]
    }
    """
    target_str = target_date.strftime("%Y-%m-%d")

    with get_connection() as conn:
        holdings = fetch_all(
            conn,
            "SELECT * FROM holdings WHERE is_closed = 0 ORDER BY entry_date",
        )

    if not holdings:
        logger.info("保有銘柄: 0件")
        return []

    results = []
    for h in holdings:
        ticker = h["ticker"]
        market = h["market"]

        current_price = _get_latest_price(ticker, target_str)
        if current_price is None:
            logger.warning(f"価格データなし: {ticker} → スキップ")
            continue

        entry_price     = float(h["entry_price"])
        stop_loss_price = float(h["stop_loss_price"]) if h["stop_loss_price"] else entry_price * (1 - _DEFAULT_SL_PCT)
        quantity        = float(h["quantity"]) if h["quantity"] else None

        pnl_pct    = (current_price - entry_price) / entry_price * 100
        pnl_amount = (current_price - entry_price) * quantity if quantity else None

        label, reasons = _judge_holding(
            ticker, target_str, current_price,
            entry_price, stop_loss_price, pnl_pct
        )

        next_earnings = _get_next_earnings(ticker, target_str)
        results.append({
            "ticker":          ticker,
            "name":            h["name"] or ticker,
            "market":          market,
            "entry_price":     entry_price,
            "entry_date":      h["entry_date"],
            "stop_loss_price": round(stop_loss_price, 2),
            "quantity":        quantity,
            "memo":            h["memo"],
            "current_price":   round(current_price, 2),
            "pnl_pct":         round(pnl_pct, 2),
            "pnl_amount":      round(pnl_amount, 0) if pnl_amount else None,
            "label":           label,
            "reasons":         reasons,
            "earnings_date":   next_earnings["date"] if next_earnings else None,
            "earnings_days":   next_earnings["days"] if next_earnings else None,
        })

    forced = sum(1 for r in results if r["label"] == "forced_stop")
    sl_near = sum(1 for r in results if r["label"] == "sl_near")
    sell    = sum(1 for r in results if r["label"] == "sell_watch")
    hold    = sum(1 for r in results if r["label"] == "hold")
    logger.info(
        f"保有銘柄判定: {len(results)} 件 | "
        f"強制損切り={forced} 売り検討={sell} SL接近={sl_near} 継続={hold}"
    )
    return results


# ============================================================
# 判定ロジック
# ============================================================

def _judge_holding(
    ticker: str,
    target_str: str,
    current_price: float,
    entry_price: float,
    stop_loss_price: float,
    pnl_pct: float,
) -> tuple[str, list[str]]:
    """
    1銘柄の保有ラベルと理由リストを返す。
    優先順位: forced_stop > sl_near > sell_watch > hold
    """
    reasons = []

    # ① 強制損切り（終値が損切りライン以下）
    if current_price <= stop_loss_price:
        reasons.append(f"終値 {current_price} が損切りライン {stop_loss_price} を下回りました")
        return "forced_stop", reasons

    # ② 損切りライン接近（残り _SL_NEAR_PCT % 以内）
    sl_remaining_pct = (current_price - stop_loss_price) / current_price * 100
    if sl_remaining_pct <= _SL_NEAR_PCT:
        reasons.append(f"損切りラインまで残り {sl_remaining_pct:.1f}%（損切り:{stop_loss_price}）")
        return "sl_near", reasons

    # ③ 売り検討条件チェック
    sell_reasons = _check_sell_conditions(ticker, target_str, current_price, entry_price, pnl_pct)
    if sell_reasons:
        return "sell_watch", sell_reasons

    # ④ 継続保有
    reasons.append("明確な売りシグナルなし")
    return "hold", reasons


def _check_sell_conditions(
    ticker: str,
    target_str: str,
    current_price: float,
    entry_price: float,
    pnl_pct: float,
) -> list[str]:
    """
    売り検討条件をチェックし、該当する理由のリストを返す。
    空リストなら売り検討なし。
    """
    reasons = []

    # MA5・MA25 を取得
    snap = _get_technical(ticker, target_str)
    if snap:
        ma5  = snap.get("ma5")
        ma25 = snap.get("ma25")
        ma5_prev  = _get_technical_5d_ago(ticker, target_str, "ma5")
        ma25_prev = _get_technical_5d_ago(ticker, target_str, "ma25")

        # MA5がMA25を下抜け（当日 MA5 < MA25 かつ 前日 MA5 >= MA25）
        if ma5 and ma25 and ma5_prev and ma25_prev:
            if ma5 < ma25 and ma5_prev >= ma25_prev:
                reasons.append("MA5がMA25を下抜けました（デッドクロス）")

        # 終値がMA25を5%以上下回った
        if ma25 and current_price < ma25 * 0.95:
            reasons.append(f"終値がMA25を{(1 - current_price/ma25)*100:.1f}%下回っています")

    # 損益率が-5%以下（損切りライン到達前の早期警戒）
    if pnl_pct <= -5.0:
        reasons.append(f"損益率が{pnl_pct:.1f}%（早期警戒水準）")

    # 利益確定圏（+10%以上）到達後、+5%以下に失速
    if _check_profit_fade(ticker, target_str, entry_price, pnl_pct):
        reasons.append(f"利益確定圏到達後に失速（現在 +{pnl_pct:.1f}%）")

    return reasons


def _check_profit_fade(
    ticker: str,
    target_str: str,
    entry_price: float,
    current_pnl_pct: float,
) -> bool:
    """
    過去に +10% 以上到達していて、現在 +5% 以下に低下していれば True。
    """
    if current_pnl_pct > 5.0:
        return False

    # price_history から最高終値を取得
    with get_connection() as conn:
        row = fetch_one(
            conn,
            "SELECT MAX(adj_close) as max_close FROM price_history "
            "WHERE ticker = ? AND date <= ? AND is_holiday = 0",
            (ticker, target_str),
        )
    if not row or not row["max_close"]:
        return False

    max_return_pct = (float(row["max_close"]) - entry_price) / entry_price * 100
    return max_return_pct >= 10.0


# ============================================================
# DB ヘルパー
# ============================================================

def _get_latest_price(ticker: str, target_str: str) -> Optional[float]:
    """指定日以前の最新終値を取得する。"""
    with get_connection() as conn:
        row = fetch_one(
            conn,
            "SELECT adj_close FROM price_history "
            "WHERE ticker = ? AND date <= ? AND is_holiday = 0 AND adj_close IS NOT NULL "
            "ORDER BY date DESC LIMIT 1",
            (ticker, target_str),
        )
    return float(row["adj_close"]) if row else None


def _get_technical(ticker: str, target_str: str) -> Optional[dict]:
    """指定日のテクニカルスナップショットを取得する。"""
    with get_connection() as conn:
        row = fetch_one(
            conn,
            "SELECT ma5, ma25 FROM technical_snapshot WHERE ticker = ? AND date = ?",
            (ticker, target_str),
        )
    return dict(row) if row else None


def _get_next_earnings(ticker: str, target_str: str) -> Optional[dict]:
    """
    earnings_schedule から当日以降で最も近い決算発表予定日を取得する。
    返り値: {"date": "YYYY-MM-DD", "days": N} または None
    """
    with get_connection() as conn:
        row = fetch_one(
            conn,
            "SELECT announcement_date FROM earnings_schedule "
            "WHERE ticker=? AND announcement_date>=? "
            "ORDER BY announcement_date ASC LIMIT 1",
            (ticker, target_str),
        )
    if not row:
        return None
    ann_date = row["announcement_date"]
    target_dt = datetime.strptime(target_str, "%Y-%m-%d").date()
    ann_dt    = datetime.strptime(ann_date,   "%Y-%m-%d").date()
    days = (ann_dt - target_dt).days
    return {"date": ann_date, "days": days}


def _get_technical_5d_ago(ticker: str, target_str: str, column: str) -> Optional[float]:
    """指定日から5営業日前のテクニカル値を取得する。"""
    with get_connection() as conn:
        row = fetch_one(
            conn,
            f"SELECT {column} FROM technical_snapshot "
            "WHERE ticker = ? AND date < ? ORDER BY date DESC LIMIT 1 OFFSET 4",
            (ticker, target_str),
        )
    return float(row[column]) if (row and row[column]) else None


def _to_float(val) -> Optional[float]:
    """文字列を float に変換する。空文字・None は None を返す。"""
    if val is None or str(val).strip() == "":
        return None
    try:
        return float(val)
    except ValueError:
        return None
