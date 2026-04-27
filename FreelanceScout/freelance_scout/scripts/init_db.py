"""
DB 初期化スクリプト

実行方法（freelance_scout/ から）:
    python -m scripts.init_db

冪等（IF NOT EXISTS）。何度実行しても安全。
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.config import CONFIG
from src.logger import get_logger
from src.data_store import get_connection

logger = get_logger(__name__)

DDL_STATEMENTS = [
    # 案件テーブル（重複排除 + 履歴）
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        source          TEXT NOT NULL CHECK(source IN ('crowdworks','coconala')),
        job_id          TEXT NOT NULL,
        title           TEXT NOT NULL,
        url             TEXT NOT NULL,
        description     TEXT,
        budget          TEXT,
        category        TEXT,
        posted_date     TEXT,
        fetched_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        notified_at     DATETIME,
        matched_keywords TEXT
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_source_job_id ON jobs(source, job_id)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_fetched ON jobs(fetched_at)",

    # バッチ実行ログ
    """
    CREATE TABLE IF NOT EXISTS batch_run_log (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        run_date          DATE NOT NULL,
        batch_type        TEXT NOT NULL DEFAULT 'morning',
        start_time        DATETIME,
        end_time          DATETIME,
        status            TEXT CHECK(status IN ('running','success','partial_failure','failed')),
        crowdworks_found  INTEGER DEFAULT 0,
        coconala_found    INTEGER DEFAULT 0,
        new_jobs          INTEGER DEFAULT 0,
        notified_jobs     INTEGER DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_batch_run_log_date ON batch_run_log(run_date)",

    # エラーログ
    """
    CREATE TABLE IF NOT EXISTS error_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        error_time    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        batch_type    TEXT,
        module        TEXT,
        error_type    TEXT,
        error_message TEXT,
        is_fatal      INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_error_log_time ON error_log(error_time)",
]


def main() -> int:
    logger.info("DB 初期化を開始します")

    db_path = Path(CONFIG["database"]["db_path"])
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with get_connection() as conn:
        for stmt in DDL_STATEMENTS:
            conn.execute(stmt)
        logger.info(f"DDL {len(DDL_STATEMENTS)} 件を実行しました")

    logger.info(f"DB 初期化完了: {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
