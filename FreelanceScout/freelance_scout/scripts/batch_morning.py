"""
朝バッチ エントリーポイント（FreelanceScout）

実行方法（freelance_scout/ から）:
    python -m scripts.batch_morning

実行タイミング: 毎日 07:00（Windowsタスクスケジューラで登録）

処理順序:
1. CrowdWorks スクレイプ
2. Coconala スクレイプ
3. キーワードフィルタ + 重複排除
4. 新着案件を DB に保存
5. Slack 通知送信
6. バッチ実行ログ更新
"""

import sys
from datetime import date, datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from shared.batch import now_str, start_batch_log, finish_batch_log
from src.config import CONFIG
from src.data_store import (
    fetch_all,
    get_connection,
    log_error,
    upsert,
)
from src.job_filter import filter_new_jobs
from src.logger import get_logger
from src.notifier import send_job_report
from src.scraper_coconala import fetch_jobs as fetch_coconala
from src.scraper_crowdworks import fetch_jobs as fetch_crowdworks

logger = get_logger(__name__)

_TODAY = date.today()
_BATCH_TYPE = "morning"


def main() -> int:
    with get_connection() as conn:
        batch_id = start_batch_log(conn, _BATCH_TYPE, _TODAY.strftime("%Y-%m-%d"),
                                   {"crowdworks_found": 0, "coconala_found": 0, "new_jobs": 0, "notified_jobs": 0})

    cw_found = 0
    cn_found = 0
    new_count = 0
    notified_count = 0

    try:
        logger.info("=" * 60)
        logger.info(f"[FreelanceScout 朝バッチ] 開始: {_TODAY}")
        logger.info("=" * 60)

        all_jobs: list[dict] = []
        has_error = False

        # ────────────────────────────────────────
        # STEP 1: CrowdWorks スクレイプ
        # ────────────────────────────────────────
        logger.info("STEP 1: CrowdWorks スクレイプ")
        try:
            cw_jobs = fetch_crowdworks()
            cw_found = len(cw_jobs)
            all_jobs.extend(cw_jobs)
            logger.info(f"CrowdWorks: {cw_found} 件取得")
        except Exception as e:
            has_error = True
            logger.error(f"CrowdWorks スクレイプ失敗: {e}", exc_info=True)
            log_error(_BATCH_TYPE, "scraper_crowdworks", type(e).__name__, str(e))

        # ────────────────────────────────────────
        # STEP 2: Coconala スクレイプ
        # ────────────────────────────────────────
        logger.info("STEP 2: Coconala スクレイプ")
        try:
            cn_jobs = fetch_coconala()
            cn_found = len(cn_jobs)
            all_jobs.extend(cn_jobs)
            logger.info(f"Coconala: {cn_found} 件取得")
        except Exception as e:
            has_error = True
            logger.error(f"Coconala スクレイプ失敗: {e}", exc_info=True)
            log_error(_BATCH_TYPE, "scraper_coconala", type(e).__name__, str(e))

        # ────────────────────────────────────────
        # STEP 3: フィルタ（重複排除 + キーワードマッチ）
        # ────────────────────────────────────────
        logger.info("STEP 3: フィルタ")
        with get_connection() as conn:
            new_jobs = filter_new_jobs(all_jobs, conn)
        new_count = len(new_jobs)
        logger.info(f"新着案件: {new_count} 件")

        # ────────────────────────────────────────
        # STEP 4: DB 保存（全取得案件を upsert）
        # ────────────────────────────────────────
        logger.info("STEP 4: DB 保存")
        now_ts = now_str()
        new_job_ids = {(j["source"], j["job_id"]) for j in new_jobs}

        db_rows = []
        for job in all_jobs:
            is_new = (job["source"], job["job_id"]) in new_job_ids
            row = {
                "source": job["source"],
                "job_id": job["job_id"],
                "title": job["title"],
                "url": job["url"],
                "description": job.get("description", ""),
                "budget": job.get("budget", ""),
                "category": job.get("category", ""),
                "posted_date": job.get("posted_date", ""),
                "notified_at": now_ts if is_new else None,
                "matched_keywords": job.get("matched_keywords", "") if is_new else None,
            }
            db_rows.append(row)

        if db_rows:
            with get_connection() as conn:
                upsert(conn, "jobs", db_rows)
            logger.info(f"DB 保存: {len(db_rows)} 件 upsert")

        # ────────────────────────────────────────
        # STEP 5: Slack 通知
        # ────────────────────────────────────────
        logger.info("STEP 5: Slack 通知")
        stats = {
            "crowdworks_found": cw_found,
            "coconala_found": cn_found,
        }
        if new_jobs:
            success = send_job_report(_TODAY, new_jobs, stats)
            notified_count = new_count if success else 0
        else:
            logger.info("新着 0 件 — 通知スキップ")

        # ────────────────────────────────────────
        # STEP 6: バッチログ更新
        # ────────────────────────────────────────
        status = "partial_failure" if has_error else "success"
        with get_connection() as conn:
            finish_batch_log(conn, batch_id, status,
                             {"crowdworks_found": cw_found, "coconala_found": cn_found,
                              "new_jobs": new_count, "notified_jobs": notified_count})
        logger.info(f"[FreelanceScout 朝バッチ] 完了: {status}")
        return 0 if status == "success" else 1

    except Exception as e:
        logger.error(f"[朝バッチ] 致命的エラー: {e}", exc_info=True)
        log_error(_BATCH_TYPE, "batch_morning", type(e).__name__, str(e), is_fatal=True)
        with get_connection() as conn:
            finish_batch_log(conn, batch_id, "failed",
                             {"crowdworks_found": cw_found, "coconala_found": cn_found,
                              "new_jobs": 0, "notified_jobs": 0})
        return 2



if __name__ == "__main__":
    sys.exit(main())
