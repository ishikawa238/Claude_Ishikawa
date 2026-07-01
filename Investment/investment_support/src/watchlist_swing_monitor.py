"""
moomoo お気に入り銘柄のスイング条件スキャンと通知ログ管理。
"""

from __future__ import annotations

from datetime import date, datetime

from src.config import CONFIG, get_swing_config
from src.data_store import fetch_all, fetch_one, get_connection, insert_one
from src.logger import get_logger
from src.moomoo_quote import iter_technical_rows, rsi_history_from_prices
from src.moomoo_watchlist import fetch_watchlist_tickers, _get_watchlist_monitor_cfg
from src.swing_detector import evaluate_watchlist_swing

logger = get_logger(__name__)


def get_monitor_config() -> dict | None:
    return _get_watchlist_monitor_cfg()


def get_latest_market_mode() -> str:
    with get_connection() as conn:
        row = fetch_one(
            conn,
            "SELECT mode FROM market_environment ORDER BY date DESC LIMIT 1",
        )
    return row["mode"] if row else "normal"


def scan_watchlist_candidates(
    markets: list[str],
    *,
    today: date | None = None,
    macd_require_both: bool = True,
) -> tuple[list[dict], int]:
    """
    お気に入り銘柄をスキャンし、厳格3条件を満たす銘柄を返す。

    返り値: (候補リスト, スキャン件数)
    """
    target = today or date.today()
    target_str = target.strftime("%Y-%m-%d")
    securities = fetch_watchlist_tickers(markets)
    if not securities:
        return [], 0

    moomoo_cfg = CONFIG.get("moomoo") or {}
    host = moomoo_cfg.get("host", "127.0.0.1")
    port = int(moomoo_cfg.get("port", 11111))
    rsi_lookback = int(get_swing_config().get("rsi_lookback_days", 5))

    candidates: list[dict] = []
    scanned = 0
    for tk in iter_technical_rows(securities, host, port, today=target):
        scanned += 1
        prices = tk.pop("_prices", [])
        rsi_hist = rsi_history_from_prices(prices, rsi_lookback) if prices else None
        row = evaluate_watchlist_swing(
            tk,
            target_str,
            macd_require_both=macd_require_both,
            rsi_history=rsi_hist,
        )
        if row:
            enriched = {**tk, **row}
            enriched["date"] = target_str
            candidates.append(enriched)

    logger.info(f"お気に入りスキャン: {scanned} 件 / 候補 {len(candidates)} 件")
    return candidates, scanned


def already_notified_today(
    ticker: str,
    market: str,
    alert_date: str,
) -> bool:
    with get_connection() as conn:
        row = fetch_one(
            conn,
            """SELECT notified FROM intraday_swing_alert_log
               WHERE alert_date = ? AND ticker = ? AND market = ?""",
            (alert_date, ticker, market),
        )
    return bool(row and row["notified"])


def filter_new_candidates(candidates: list[dict]) -> list[dict]:
    """その日未通知の候補のみ返す。"""
    new_items: list[dict] = []
    for c in candidates:
        alert_date = c.get("date") or date.today().strftime("%Y-%m-%d")
        if already_notified_today(c["ticker"], c["market"], alert_date):
            continue
        new_items.append(c)
    return new_items


def upsert_check_log(candidate: dict, *, notified: bool = False) -> None:
    """スキャン結果を intraday_swing_alert_log に記録する。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    alert_date = candidate.get("date") or date.today().strftime("%Y-%m-%d")
    row = {
        "alert_date": alert_date,
        "ticker": candidate["ticker"],
        "market": candidate["market"],
        "label": candidate.get("label") or "candidate",
        "close_price": candidate.get("close"),
        "ema25": candidate.get("ema25"),
        "ema75": candidate.get("ema75"),
        "rsi14": candidate.get("rsi14"),
        "macd_diff": candidate.get("macd_diff"),
        "macd_dea": candidate.get("macd_dea"),
        "macd_hist": candidate.get("macd_hist"),
        "vol_ratio": candidate.get("vol_ratio"),
        "atr14": candidate.get("atr14"),
        "macd_confirm_type": candidate.get("macd_confirm_type"),
        "notified": 1 if notified else 0,
        "last_check_at": now,
    }
    with get_connection() as conn:
        existing = fetch_one(
            conn,
            """SELECT id, first_alert_at, notified FROM intraday_swing_alert_log
               WHERE alert_date = ? AND ticker = ? AND market = ?""",
            (alert_date, row["ticker"], row["market"]),
        )
        if existing:
            row["first_alert_at"] = existing["first_alert_at"]
            if notified:
                row["notified"] = 1
                if not existing["first_alert_at"]:
                    row["first_alert_at"] = now
            elif existing["notified"]:
                row["notified"] = 1
            cols = [k for k in row.keys()]
            sets = ", ".join(f"{c}=?" for c in cols)
            conn.execute(
                f"UPDATE intraday_swing_alert_log SET {sets} WHERE id=?",
                tuple(row[c] for c in cols) + (existing["id"],),
            )
        else:
            row["first_alert_at"] = now if notified else None
            insert_one(conn, "intraday_swing_alert_log", row)


def mark_notified(candidates: list[dict]) -> None:
    for c in candidates:
        upsert_check_log(c, notified=True)


def fetch_todays_alert_log(alert_date: str) -> list[dict]:
    with get_connection() as conn:
        return fetch_all(
            conn,
            "SELECT * FROM intraday_swing_alert_log WHERE alert_date = ? ORDER BY ticker",
            (alert_date,),
        )
