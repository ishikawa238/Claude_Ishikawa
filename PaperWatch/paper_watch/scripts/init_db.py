"""
DB 初期化スクリプト

実行方法（paper_watch/ から）:
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
    """
    CREATE TABLE IF NOT EXISTS papers (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        arxiv_id        TEXT NOT NULL UNIQUE,
        title           TEXT NOT NULL,
        abstract        TEXT,
        authors         TEXT,
        url             TEXT NOT NULL,
        category        TEXT,
        score           INTEGER DEFAULT 0,
        matched_keywords TEXT,
        fetched_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        notified_at     DATETIME
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_papers_fetched ON papers(fetched_at)",
    "CREATE INDEX IF NOT EXISTS idx_papers_score ON papers(score)",

    """
    CREATE TABLE IF NOT EXISTS batch_run_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        run_date        DATE NOT NULL,
        batch_type      TEXT NOT NULL DEFAULT 'morning',
        start_time      DATETIME,
        end_time        DATETIME,
        status          TEXT CHECK(status IN ('running','success','partial_failure','failed')),
        papers_found    INTEGER DEFAULT 0,
        papers_new      INTEGER DEFAULT 0,
        papers_notified INTEGER DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_batch_run_log_date ON batch_run_log(run_date)",

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
]


def main() -> int:
    logger.info("DB 初期化を開始します")

    db_path = Path(CONFIG["database"]["db_path"])
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with get_connection() as conn:
        for stmt in DDL_STATEMENTS:
            conn.execute(stmt)
        logger.info(f"DDL {len(DDL_STATEMENTS)} 件を実行しました")

        # 既存 DB のマイグレーション: batch_run_log に batch_type カラムを追加
        try:
            conn.execute(
                "ALTER TABLE batch_run_log ADD COLUMN batch_type TEXT DEFAULT 'morning'"
            )
            logger.info("マイグレーション: batch_run_log.batch_type カラムを追加しました")
        except Exception:
            # カラムが既に存在する場合は無視
            pass

    logger.info(f"DB 初期化完了: {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
