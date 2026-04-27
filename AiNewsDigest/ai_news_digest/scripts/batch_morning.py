"""
朝バッチスクリプト — AI ニュースダイジェスト

実行方法（ai_news_digest/ から）:
    python -m scripts.batch_morning

処理フロー:
    1. RSS フィード取得
    2. 日付フィルタ + 重複排除
    3. DB 保存
    4. Slack 通知
"""

import sys
from datetime import date, datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from shared.batch import now_str, start_batch_log, finish_batch_log
from src.config import CONFIG
from src.data_store import get_connection, log_error, upsert
from src.logger import get_logger
from src.news_filter import filter_new_articles
from src.notifier import send_news_digest
from src.rss_fetcher import fetch_all_feeds

logger = get_logger(__name__)
_TODAY = date.today()
_BATCH_TYPE = "morning"


def main() -> int:
    with get_connection() as conn:
        batch_id = start_batch_log(conn, _BATCH_TYPE, _TODAY.strftime("%Y-%m-%d"))

    total_fetched = 0
    new_count = 0
    notified_count = 0

    try:
        logger.info("=" * 60)
        logger.info(f"[AiNewsDigest 朝バッチ] 開始: {_TODAY}")
        logger.info("=" * 60)

        # STEP 1: RSS フィード取得
        logger.info("STEP 1: RSS フィード取得")
        try:
            all_articles = fetch_all_feeds()
            total_fetched = len(all_articles)
            logger.info(f"取得合計: {total_fetched} 件")
        except Exception as e:
            logger.error(f"RSS 取得失敗: {e}", exc_info=True)
            log_error(_BATCH_TYPE, "rss_fetcher", type(e).__name__, str(e))
            all_articles = []

        # STEP 2: フィルタ（日付 + 重複排除）
        logger.info("STEP 2: フィルタ")
        new_articles = filter_new_articles(all_articles)
        new_count = len(new_articles)
        logger.info(f"新着記事: {new_count} 件")

        # STEP 3: DB 保存
        logger.info("STEP 3: DB 保存")
        now_ts = now_str()
        new_urls = {a["url"] for a in new_articles}

        db_rows = []
        for article in all_articles:
            is_new = article["url"] in new_urls
            row = {
                "url": article["url"],
                "title": article["title"],
                "summary": article.get("summary", ""),
                "source": article.get("source", ""),
                "published_at": article.get("published_at"),
                "notified_at": now_ts if is_new else None,
            }
            db_rows.append(row)

        if db_rows:
            with get_connection() as conn:
                upsert(conn, "articles", db_rows)
            logger.info(f"DB 保存: {len(db_rows)} 件 upsert")

        # STEP 4: Slack 通知
        logger.info("STEP 4: Slack 通知")
        stats = {"total_fetched": total_fetched}
        if new_articles:
            success = send_news_digest(_TODAY, new_articles, stats)
            notified_count = new_count if success else 0
        else:
            logger.info("新着 0 件 — 通知スキップ")

        # STEP 5: バッチログ更新
        with get_connection() as conn:
            finish_batch_log(conn, batch_id, "success",
                             {"total_fetched": total_fetched, "new_articles": new_count, "notified": notified_count})
        logger.info(f"[AiNewsDigest 朝バッチ] 完了: success")
        return 0

    except Exception as e:
        logger.error(f"[朝バッチ] 致命的エラー: {e}", exc_info=True)
        log_error(_BATCH_TYPE, "batch_morning", type(e).__name__, str(e), is_fatal=True)
        with get_connection() as conn:
            finish_batch_log(conn, batch_id, "failed",
                             {"total_fetched": total_fetched, "new_articles": 0, "notified": 0})
        return 2



if __name__ == "__main__":
    sys.exit(main())
