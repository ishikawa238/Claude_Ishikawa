"""
夜バッチ（日本株）エントリーポイント

実行方法（investment_support/ から）:
    python -m scripts.batch_evening_jp

実行タイミング: 毎日 20:30（Windowsタスクスケジューラで登録）

処理順序:
1. 日本株 + インデックス（N225/TOPIX）の価格データ取得
2. テクニカル指標計算
3. 市場環境判定（日本株分）
4. 流動性フィルタ
5. パターンA/B シグナル検出
6. エントリーセット計算
7. 保有銘柄判定
8. 夜通知（Slack）送信
9. DBバックアップ
10. バッチ実行ログ更新
"""

import sys
from datetime import date, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent  # Ishikawa238/ — shared パッケージの検索に必要
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

from shared.batch import start_batch_log, finish_batch_log
from src.config import CONFIG
from src.data_fetcher import fetch_index_history
from src.yfinance_fetcher import fetch_daily_yf
from src.data_store import (
    backup_database, fetch_all, get_connection,
    log_error, upsert,
)
from src.entry_calculator import save_entry_sets
from src.holdings_monitor import ensure_holding_prices, evaluate_holdings, sync_holdings_from_csv
from src.logger import get_logger
from src.market_filter import get_screened_tickers, judge_market_environment
from src.notifier import send_evening_report
from src.signal_detector import detect_and_save_signals
from src.technical_calculator import (
    calculate_and_save_index_mas, calculate_and_save_snapshots,
)

logger = get_logger(__name__)
_TODAY = date.today()
_BATCH_TYPE = "evening_jp"


def main() -> int:
    """
    夜バッチ全体を実行する。
    返り値: 0=成功 / 1=部分失敗 / 2=致命的エラー
    """
    with get_connection() as conn:
        batch_id = start_batch_log(conn, _BATCH_TYPE, _TODAY.strftime("%Y-%m-%d"),
                                   {"total_tickers": 0, "failed_tickers": 0, "signals_generated": 0})
    failed_count = 0
    signals_count = 0

    try:
        # ────────────────────────────────────────
        # STEP 1: 日本株 + JP インデックス 価格取得
        # ────────────────────────────────────────
        logger.info("=" * 60)
        logger.info(f"[夜バッチ] 開始: {_TODAY}")
        logger.info("=" * 60)

        # JP インデックス取得
        for code in ("N225", "TOPIX"):
            rows = fetch_index_history(code, _TODAY - timedelta(days=5), _TODAY)
            if rows:
                with get_connection() as conn:
                    upsert(conn, "index_snapshot", rows)

        # JP 銘柄一覧取得（テクニカル計算用）
        with get_connection() as conn:
            jp_tickers = fetch_all(
                conn,
                "SELECT ticker FROM universe_master "
                "WHERE market='JP' AND is_active=1 AND is_excluded=0",
            )
        tickers_with_market = [(r["ticker"], "JP") for r in jp_tickers]
        logger.info(f"対象銘柄: JP {len(tickers_with_market)} 件")

        # 価格データ取得（yfinance: EODHD JP Bulk が利用不可のため代替）
        all_rows = fetch_daily_yf(tickers_with_market, _TODAY)
        failed_count = 0
        if not all_rows:
            logger.warning("JP 価格取得失敗。price_history の更新をスキップします。")
            failed_count = len(tickers_with_market)
        else:
            _save_in_chunks(all_rows, "price_history")

        # ────────────────────────────────────────
        # STEP 2: テクニカル指標計算
        # ────────────────────────────────────────
        logger.info("STEP 2: テクニカル指標計算")
        calculate_and_save_snapshots(tickers_with_market, _TODAY)
        calculate_and_save_index_mas("N225", _TODAY)
        calculate_and_save_index_mas("TOPIX", _TODAY)

        # ────────────────────────────────────────
        # STEP 3: 市場環境判定（JP のみ）
        # ────────────────────────────────────────
        logger.info("STEP 3: 市場環境判定")
        market_env = judge_market_environment(_TODAY, market="JP")
        mode = market_env["mode"]

        # ────────────────────────────────────────
        # STEP 4-6: スクリーニング → シグナル → エントリーセット
        # ────────────────────────────────────────
        logger.info("STEP 4: 流動性フィルタ")
        screened = get_screened_tickers(_TODAY, "JP")

        logger.info("STEP 5: シグナル検出")
        candidates = detect_and_save_signals(screened, _TODAY, mode)
        signals_count = len(candidates)

        logger.info("STEP 6: エントリーセット計算")
        entry_sets = save_entry_sets(candidates, mode)

        # ────────────────────────────────────────
        # STEP 7: 保有銘柄判定
        # ────────────────────────────────────────
        logger.info("STEP 7: 保有銘柄判定")
        sync_holdings_from_csv()
        ensure_holding_prices(_TODAY)  # ETF等 universe_master 未登録銘柄の価格を補完
        _update_earnings_schedule_for_holdings()
        holdings = evaluate_holdings(_TODAY)

        # ────────────────────────────────────────
        # STEP 8: 夜通知送信
        # ────────────────────────────────────────
        logger.info("STEP 8: 夜通知送信")
        send_evening_report(_TODAY, market_env, entry_sets, holdings, failed_count)

        # ────────────────────────────────────────
        # STEP 9: DBバックアップ
        # ────────────────────────────────────────
        logger.info("STEP 9: DBバックアップ")
        backup_database()

        # ────────────────────────────────────────
        # STEP 10: バッチログ更新
        # ────────────────────────────────────────
        status = "partial_failure" if failed_count > 0 else "success"
        with get_connection() as conn:
            finish_batch_log(conn, batch_id, status,
                             {"total_tickers": len(tickers_with_market), "failed_tickers": failed_count, "signals_generated": signals_count})
        logger.info(f"[夜バッチ] 完了: {status}")
        return 0 if status == "success" else 1

    except Exception as e:
        logger.error(f"[夜バッチ] 致命的エラー: {e}", exc_info=True)
        log_error(_BATCH_TYPE, "batch_evening_jp", type(e).__name__, str(e), is_fatal=True)
        with get_connection() as conn:
            finish_batch_log(conn, batch_id, "failed",
                             {"total_tickers": 0, "failed_tickers": 0, "signals_generated": 0})
        return 2


# ============================================================
# ユーティリティ
# ============================================================

def _update_earnings_schedule_for_holdings() -> None:
    """保有中の JP 銘柄（最大3件程度）の決算予定日を J-Quants から更新する。"""
    from src.jquants_fetcher import fetch_and_save_earnings_schedule
    with get_connection() as conn:
        rows = fetch_all(
            conn,
            "SELECT DISTINCT h.ticker FROM holdings h "
            "INNER JOIN universe_master u ON h.ticker = u.ticker "
            "WHERE h.is_closed=0 AND u.market='JP'",
        )
    jp_tickers = [r["ticker"] for r in rows]
    if jp_tickers:
        fetch_and_save_earnings_schedule(jp_tickers, _TODAY)


def _save_in_chunks(rows: list, table: str, chunk: int = 5000) -> None:
    for i in range(0, len(rows), chunk):
        with get_connection() as conn:
            upsert(conn, table, rows[i:i + chunk])




if __name__ == "__main__":
    sys.exit(main())
