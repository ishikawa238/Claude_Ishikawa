"""
月次バッチ エントリーポイント

実行方法（investment_support/ ディレクトリから）:
    python -m scripts.batch_monthly

実行タイミング: 毎月第1営業日 07:30（Windowsタスクスケジューラで登録）
               または任意のタイミングで手動実行

処理順序:
1. 銘柄マスタ更新（EODHD から再取得・新規上場／上場廃止の反映）
2. 前月の月次レポート生成・Slack 送信
3. バッチ実行ログ更新
"""

import sys
from datetime import date
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent  # Ishikawa238/ — shared パッケージの検索に必要
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

from shared.batch import start_batch_log, finish_batch_log
from src.data_fetcher import fetch_exchange_symbols
from src.data_store import (
    fetch_all, get_connection, log_error, upsert,
)
from src.jquants_fetcher import fetch_and_save_fundamentals
from src.logger import get_logger
from src.report_generator import send_monthly_report

logger = get_logger(__name__)
_TODAY      = date.today()
_BATCH_TYPE = "monthly"


def main() -> int:
    with get_connection() as conn:
        batch_id = start_batch_log(conn, _BATCH_TYPE, _TODAY.strftime("%Y-%m-%d"),
                                   {"total_tickers": 0, "failed_tickers": 0, "signals_generated": 0})

    try:
        logger.info("=" * 60)
        logger.info(f"[月次バッチ] 開始: {_TODAY}")
        logger.info("=" * 60)

        # ────────────────────────────────────────
        # STEP 1: 銘柄マスタ更新
        # ────────────────────────────────────────
        logger.info("STEP 1: 銘柄マスタ更新")
        _update_universe()

        # ────────────────────────────────────────
        # STEP 1.5: 財務データ更新（J-Quants, JP のみ）
        # ────────────────────────────────────────
        logger.info("STEP 1.5: 財務データ更新（J-Quants）")
        with get_connection() as conn:
            jp_tickers = [
                r["ticker"] for r in fetch_all(
                    conn,
                    "SELECT ticker FROM universe_master "
                    "WHERE market='JP' AND is_active=1 AND is_excluded=0",
                )
            ]
        fetch_and_save_fundamentals(jp_tickers, _TODAY)

        # ────────────────────────────────────────
        # STEP 2: 前月レポート送信
        # ────────────────────────────────────────
        logger.info("STEP 2: 月次レポート送信")
        prev_year, prev_month = _prev_month(_TODAY.year, _TODAY.month)
        send_monthly_report(prev_year, prev_month)

        # ────────────────────────────────────────
        # STEP 3: バッチログ更新
        # ────────────────────────────────────────
        with get_connection() as conn:
            finish_batch_log(conn, batch_id, "success")
        logger.info("[月次バッチ] 完了")
        return 0

    except Exception as e:
        logger.error(f"[月次バッチ] 致命的エラー: {e}", exc_info=True)
        log_error(_BATCH_TYPE, "batch_monthly", type(e).__name__, str(e), is_fatal=True)
        with get_connection() as conn:
            finish_batch_log(conn, batch_id, "failed")
        return 2


# ============================================================
# 銘柄マスタ更新
# ============================================================

def _update_universe() -> None:
    """
    EODHD から最新銘柄一覧を取得し、universe_master を更新する。
    - 新規上場: INSERT
    - 既存銘柄: last_updated のみ更新（スクリーニング条件は変わらない）
    - 上場廃止: EODHD の一覧に含まれなくなった銘柄を is_active=0 に更新
    """
    for market in ["JP", "US"]:
        logger.info(f"  {market} 銘柄マスタ取得中...")
        new_symbols = fetch_exchange_symbols(market)
        if not new_symbols:
            logger.warning(f"  {market}: 銘柄マスタ取得失敗。スキップします。")
            continue

        new_tickers = {s["ticker"] for s in new_symbols}

        # 既存の全銘柄を取得
        with get_connection() as conn:
            existing = fetch_all(
                conn,
                "SELECT ticker FROM universe_master WHERE market=?",
                (market,),
            )
        existing_tickers = {r["ticker"] for r in existing}

        # 上場廃止（既存にあって新しい一覧にない銘柄）を非アクティブ化
        delisted = existing_tickers - new_tickers
        if delisted:
            with get_connection() as conn:
                conn.execute(
                    f"UPDATE universe_master SET is_active=0 "
                    f"WHERE market=? AND ticker IN ({','.join('?' * len(delisted))})",
                    (market, *delisted),
                )
            logger.info(f"  {market}: 上場廃止 {len(delisted)} 銘柄を非アクティブ化")

        # 新規上場 + 既存銘柄の last_updated を UPSERT
        with get_connection() as conn:
            upsert(conn, "universe_master", new_symbols)

        added = new_tickers - existing_tickers
        logger.info(
            f"  {market}: 合計 {len(new_symbols)} 銘柄 "
            f"（新規 {len(added)}件 / 廃止 {len(delisted)}件）"
        )


# ============================================================
# ユーティリティ
# ============================================================

def _prev_month(year: int, month: int) -> tuple[int, int]:
    if month == 1:
        return year - 1, 12
    return year, month - 1




if __name__ == "__main__":
    sys.exit(main())
