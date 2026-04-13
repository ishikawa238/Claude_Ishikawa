"""
朝バッチ（米国株含む全体）エントリーポイント

実行方法（investment_support/ から）:
    python -m scripts.batch_morning_all

実行タイミング: 毎平日 07:00（Windowsタスクスケジューラで登録）

処理順序:
1. 米国株 + インデックス（SP500/NASDAQ）の価格データ取得
2. 米国株テクニカル指標計算
3. 市場環境モード確定（前夜バッチの JP + 今朝の US を統合）
4. 米国株: 流動性フィルタ → シグナル検出 → エントリーセット計算
5. 保有銘柄判定（JP+US 全体）
6. 朝通知（Slack）送信
7. バッチ実行ログ更新
"""

import sys
from datetime import date, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.config import CONFIG
from src.data_fetcher import fetch_bulk_eod, fetch_index_history
from src.data_store import (
    fetch_all, fetch_one, get_connection,
    insert_one, log_error, upsert,
)
from src.entry_calculator import save_entry_sets
from src.holdings_monitor import evaluate_holdings, sync_holdings_from_csv
from src.logger import get_logger
from src.market_filter import get_screened_tickers, judge_market_environment
from src.notifier import send_morning_report
from src.signal_detector import detect_and_save_signals
from src.technical_calculator import (
    calculate_and_save_index_mas, calculate_and_save_snapshots,
)

logger = get_logger(__name__)
_TODAY = date.today()
_BATCH_TYPE = "morning_all"


def main() -> int:
    start_time = _now_str()
    batch_id = _start_batch_log(start_time)
    failed_count = 0
    signals_count = 0

    try:
        logger.info("=" * 60)
        logger.info(f"[朝バッチ] 開始: {_TODAY}")
        logger.info("=" * 60)

        # ────────────────────────────────────────
        # STEP 1: 米国株 + US インデックス 価格取得
        # ────────────────────────────────────────
        for code in ("SP500", "NASDAQ"):
            rows = fetch_index_history(code, _TODAY - timedelta(days=5), _TODAY)
            if rows:
                with get_connection() as conn:
                    upsert(conn, "index_snapshot", rows)

        with get_connection() as conn:
            us_tickers = fetch_all(
                conn,
                "SELECT ticker FROM universe_master "
                "WHERE market='US' AND is_active=1 AND is_excluded=0",
            )
        tickers_with_market = [(r["ticker"], "US") for r in us_tickers]
        logger.info(f"対象銘柄: US {len(tickers_with_market)} 件")

        # 価格データ取得（Bulk EOD: 1リクエストで取引所全銘柄）
        all_rows = fetch_bulk_eod("US", _TODAY)
        failed_count = 0
        if not all_rows:
            logger.warning("US Bulk EOD 取得失敗。price_history の更新をスキップします。")
            failed_count = len(tickers_with_market)
        else:
            _save_in_chunks(all_rows, "price_history")

        # ────────────────────────────────────────
        # STEP 2: 米国株テクニカル指標計算
        # ────────────────────────────────────────
        logger.info("STEP 2: テクニカル指標計算（US）")
        calculate_and_save_snapshots(tickers_with_market, _TODAY)
        calculate_and_save_index_mas("SP500",  _TODAY)
        calculate_and_save_index_mas("NASDAQ", _TODAY)

        # ────────────────────────────────────────
        # STEP 3: 市場環境モード確定
        # ────────────────────────────────────────
        logger.info("STEP 3: 市場環境モード確定（US 確定 → 統合）")
        market_env = judge_market_environment(_TODAY, market="ALL")
        mode = market_env["mode"]

        # ────────────────────────────────────────
        # STEP 4: 米国株シグナル検出
        # ────────────────────────────────────────
        us_entry_sets = []
        if mode == "normal":
            logger.info("STEP 4: 流動性フィルタ・シグナル検出（US）")
            screened = get_screened_tickers(_TODAY, "US")
            candidates = detect_and_save_signals(screened, _TODAY, mode)
            signals_count = len(candidates)
            us_entry_sets = save_entry_sets(candidates, mode)
        else:
            logger.info(f"市場環境 [{mode}]: STEP 4 スキップ")

        # 前夜バッチの JP entry_sets も取得（朝通知の上位2件に含める）
        jp_entry_sets = _load_last_evening_entry_sets()
        all_entry_sets = jp_entry_sets + us_entry_sets

        # ────────────────────────────────────────
        # STEP 5: 保有銘柄判定（JP+US 全体）
        # ────────────────────────────────────────
        logger.info("STEP 5: 保有銘柄判定")
        sync_holdings_from_csv()
        holdings = evaluate_holdings(_TODAY)

        # ────────────────────────────────────────
        # STEP 6: 朝通知送信
        # ────────────────────────────────────────
        logger.info("STEP 6: 朝通知送信")
        send_morning_report(_TODAY, market_env, all_entry_sets, holdings)

        # ────────────────────────────────────────
        # STEP 7: バッチログ更新
        # ────────────────────────────────────────
        status = "partial_failure" if failed_count > 0 else "success"
        _finish_batch_log(batch_id, status, len(tickers_with_market), failed_count, signals_count)
        logger.info(f"[朝バッチ] 完了: {status}")
        return 0 if status == "success" else 1

    except Exception as e:
        logger.error(f"[朝バッチ] 致命的エラー: {e}", exc_info=True)
        log_error(_BATCH_TYPE, "batch_morning_all", type(e).__name__, str(e), is_fatal=True)
        _finish_batch_log(batch_id, "failed", 0, 0, 0)
        return 2


# ============================================================
# 前夜バッチの JP エントリーセット取得
# ============================================================

def _load_last_evening_entry_sets() -> list[dict]:
    """
    前夜バッチで保存された JP の trade_signal_log を取得して
    朝通知用の形式で返す。
    """
    today_str = _TODAY.strftime("%Y-%m-%d")
    with get_connection() as conn:
        rows = fetch_all(
            conn,
            "SELECT * FROM trade_signal_log "
            "WHERE signal_date = ? AND market = 'JP' "
            "ORDER BY id DESC LIMIT 3",
            (today_str,),
        )
    return [dict(r) for r in rows]


# ============================================================
# ユーティリティ（batch_evening_jp と同一）
# ============================================================

def _save_in_chunks(rows: list, table: str, chunk: int = 5000) -> None:
    for i in range(0, len(rows), chunk):
        with get_connection() as conn:
            upsert(conn, table, rows[i:i + chunk])


def _now_str() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _start_batch_log(start_time: str) -> int:
    with get_connection() as conn:
        return insert_one(conn, "batch_run_log", {
            "run_date":          _TODAY.strftime("%Y-%m-%d"),
            "batch_type":        _BATCH_TYPE,
            "start_time":        start_time,
            "status":            "running",
            "total_tickers":     0,
            "failed_tickers":    0,
            "signals_generated": 0,
        })


def _finish_batch_log(
    batch_id: int, status: str,
    total: int, failed: int, signals: int,
) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE batch_run_log SET status=?, end_time=?, "
            "total_tickers=?, failed_tickers=?, signals_generated=? "
            "WHERE id=?",
            (status, _now_str(), total, failed, signals, batch_id),
        )


if __name__ == "__main__":
    sys.exit(main())
