"""
朝バッチスクリプト

実行方法（paper_watch/ から）:
    python -m scripts.batch_morning

フロー:
    RSS取得 → 重複排除(DB) → スコアリング → DB保存 → Slack通知
"""

import sys
from datetime import date, datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from shared.batch import start_batch_log, finish_batch_log
from src.config import CONFIG
from src.data_store import fetch_all, get_connection, upsert
from src.logger import get_logger
from src.notifier import notify_papers
from src.rss_fetcher import fetch_new_papers
from src.scorer import score_papers

logger = get_logger(__name__)

_TODAY = date.today()
_BATCH_TYPE = "morning"


def _filter_new_papers(conn, papers: list[dict]) -> list[dict]:
    """DB に未登録の論文だけを返す。"""
    if not papers:
        return []
    existing = fetch_all(conn, "SELECT arxiv_id FROM papers")
    existing_ids = {row["arxiv_id"] for row in existing}
    return [p for p in papers if p["arxiv_id"] not in existing_ids]


def main() -> int:
    logger.info("=== PaperWatch 朝バッチ開始 ===")
    stats = {"found": 0, "new": 0, "notified": 0}

    with get_connection() as conn:
        log_id = start_batch_log(conn, _BATCH_TYPE, _TODAY.strftime("%Y-%m-%d"))

    status = "failed"
    try:
        # 1. RSS 取得
        all_papers = fetch_new_papers()
        stats["found"] = len(all_papers)
        logger.info(f"取得論文数: {stats['found']}")

        # 2. 重複排除
        with get_connection() as conn:
            new_papers = _filter_new_papers(conn, all_papers)
        stats["new"] = len(new_papers)
        logger.info(f"新規論文数: {stats['new']}")

        if not new_papers:
            logger.info("新規論文はありません")
            status = "success"
            return 0

        # 3. スコアリング
        scored = score_papers(new_papers)

        # 4. DB 保存
        with get_connection() as conn:
            upsert(conn, "papers", [
                {
                    "arxiv_id": p["arxiv_id"],
                    "title": p["title"],
                    "abstract": p.get("abstract", ""),
                    "authors": p.get("authors", ""),
                    "url": p["url"],
                    "category": p.get("category", ""),
                    "score": p["score"],
                    "matched_keywords": p.get("matched_keywords", ""),
                }
                for p in scored
            ])
        logger.info(f"DB 保存完了: {len(scored)} 件")

        # 5. 閾値超の論文を通知
        threshold = CONFIG.get("scoring", {}).get("score_threshold", 30)
        to_notify = [p for p in scored if p["score"] >= threshold]
        logger.info(f"閾値({threshold})以上: {len(to_notify)} 件")

        if to_notify:
            ok = notify_papers(to_notify)
            stats["notified"] = len(to_notify) if ok else 0
            # 通知済みフラグを更新
            if ok:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with get_connection() as conn:
                    for p in to_notify:
                        conn.execute(
                            "UPDATE papers SET notified_at=? WHERE arxiv_id=?",
                            (now, p["arxiv_id"]),
                        )

        status = "success"
        return 0

    except Exception as e:
        logger.error(f"バッチ実行エラー: {e}", exc_info=True)
        status = "failed"
        return 1

    finally:
        with get_connection() as conn:
            finish_batch_log(conn, log_id, status,
                             {"papers_found": stats.get("found", 0),
                              "papers_new": stats.get("new", 0),
                              "papers_notified": stats.get("notified", 0)})
        logger.info(f"=== PaperWatch 朝バッチ終了 (status={status}) ===")


if __name__ == "__main__":
    sys.exit(main())
