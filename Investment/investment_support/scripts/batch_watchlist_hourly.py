"""
お気に入り銘柄の毎時スイング監視 + 保有銘柄の損切り・利確アラート

実行方法（investment_support/ から）:
    python -m scripts.batch_watchlist_hourly
"""

from __future__ import annotations

import atexit
import os
import sys
from datetime import date
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

from shared.batch import finish_batch_log, start_batch_log
from src.data_store import get_connection, log_error
from src.holdings_monitor import (
    evaluate_holdings,
    fetch_live_snapshots_for_holdings,
    filter_new_holding_alerts,
    mark_holding_alerts_notified,
    refresh_dashboard_from_live,
    sync_holdings_from_csv,
)
from src.logger import get_logger
from src.market_hours import open_markets
from src.notifier import send_holdings_intraday_alert, send_watchlist_swing_alert
from src.watchlist_swing_monitor import (
    filter_new_candidates,
    get_latest_market_mode,
    get_monitor_config,
    mark_notified,
    scan_watchlist_candidates,
)

logger = get_logger(__name__)
_TODAY = date.today()
_ALERT_DATE = _TODAY.strftime("%Y-%m-%d")
_BATCH_TYPE = "watchlist_hourly"
_LOCK_DIR = _PROJECT_ROOT / "data"
_LOCK_FILE = _LOCK_DIR / "batch_watchlist_hourly.lock"


def _acquire_lock():
    _LOCK_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fp = open(_LOCK_FILE, "w")
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fp.write(str(os.getpid()))
        fp.flush()
        return fp
    except (OSError, IOError):
        return None


def _release_lock(fp) -> None:
    if fp is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            fp.seek(0)
            msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fp, fcntl.LOCK_UN)
        fp.close()
    except (OSError, IOError):
        pass


def _run_batch() -> int:
    mon = get_monitor_config()
    if not mon:
        logger.info("[毎時監視] watchlist_monitor.enabled=false のためスキップ")
        return 0

    markets = [m.upper() for m in (mon.get("markets") or ["US"])]
    active = open_markets(markets)
    if not active:
        logger.info(f"[毎時監視] 市場クローズ ({markets}) のためスキップ")
        return 5

    macd_both = bool(mon.get("macd_require_both", True))
    max_alerts = max(int(mon.get("max_alerts_per_run", 10)), 1)
    respect_market_mode = bool(mon.get("respect_market_mode", True))
    market_mode = get_latest_market_mode()
    swing_paused = respect_market_mode and market_mode in ("caution", "full_caution")

    with get_connection() as conn:
        batch_id = start_batch_log(conn, _BATCH_TYPE, _ALERT_DATE)

    status = "success"
    swing_signals = 0
    holding_alerts = 0
    scanned = 0
    try:
        sync_holdings_from_csv()

        # --- ダッシュボード: 現値・総資産サマリー更新 ---
        live_prices, live_snapshots = fetch_live_snapshots_for_holdings(active)
        dashboard = refresh_dashboard_from_live(live_snapshots, _TODAY, batch_type=_BATCH_TYPE)

        # --- 保有銘柄: 損切り・利確（市場環境に関わらず常に監視） ---
        holdings = evaluate_holdings(_TODAY, live_prices=live_prices or None)
        holdings_in_market = [h for h in holdings if h["market"] in active]
        new_holding_alerts = filter_new_holding_alerts(holdings_in_market, _ALERT_DATE)
        holding_alerts = len(new_holding_alerts)
        if new_holding_alerts:
            send_holdings_intraday_alert(_TODAY, new_holding_alerts)
            mark_holding_alerts_notified(new_holding_alerts)
            logger.info(f"[毎時監視] 保有アラート Slack 通知: {holding_alerts} 件")

        # --- お気に入り: スイング候補（警戒モード時はスキップ） ---
        if swing_paused:
            logger.info(f"[毎時監視] 市場環境={market_mode} のためスイング候補通知をスキップ")
        else:
            candidates, scanned = scan_watchlist_candidates(
                active,
                today=_TODAY,
                macd_require_both=macd_both,
            )
            new_candidates = filter_new_candidates(candidates)[:max_alerts]
            swing_signals = len(new_candidates)
            if new_candidates:
                market_label = "/".join(active)
                send_watchlist_swing_alert(_TODAY, new_candidates, market_label=market_label)
                mark_notified(new_candidates)
                logger.info(f"[毎時監視] スイング候補 Slack 通知: {swing_signals} 件")
            else:
                logger.info(
                    f"[毎時監視] 新規スイング候補なし "
                    f"(スキャン {scanned} / 候補 {len(candidates)})"
                )

        total_signals = swing_signals + holding_alerts
        if not total_signals:
            logger.info("[毎時監視] 通知対象なし")

        with get_connection() as conn:
            finish_batch_log(
                conn,
                batch_id,
                status=status,
                extra={
                    "total_tickers": scanned,
                    "signals_generated": total_signals,
                    "memo": (
                        f"markets={','.join(active)};"
                        f"holdings={holding_alerts};swing={swing_signals};"
                        f"dashboard_prices={dashboard.get('prices_updated', 0)}"
                    ),
                },
            )
        return 0
    except Exception as e:
        logger.exception(f"[毎時監視] エラー: {e}")
        log_error(_BATCH_TYPE, str(e), ticker=None)
        with get_connection() as conn:
            finish_batch_log(
                conn,
                batch_id,
                status="failed",
                extra={
                    "total_tickers": scanned,
                    "signals_generated": swing_signals + holding_alerts,
                },
            )
        return 2


def main() -> int:
    lock_fp = _acquire_lock()
    if lock_fp is None:
        logger.warning("[毎時監視] 二重起動のためスキップ")
        return 3
    atexit.register(_release_lock, lock_fp)
    return _run_batch()


if __name__ == "__main__":
    raise SystemExit(main())
