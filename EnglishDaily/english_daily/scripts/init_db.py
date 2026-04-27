"""
DB 初期化スクリプト

実行方法（english_daily/ から）:
    python -m scripts.init_db

冪等（IF NOT EXISTS）。何度実行しても安全。
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.config import CONFIG
from src.data_store import get_connection
from src.logger import get_logger

logger = get_logger(__name__)

DDL_STATEMENTS = [
    # 朝の配信メッセージ（リアクション確認用に ts を保持）
    """
    CREATE TABLE IF NOT EXISTS daily_message (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        date        DATE    NOT NULL UNIQUE,
        channel_id  TEXT    NOT NULL,
        message_ts  TEXT    NOT NULL,
        task_count  INTEGER NOT NULL,
        created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_daily_message_date ON daily_message(date)",

    # ストリーク（単一行: id=1）
    """
    CREATE TABLE IF NOT EXISTS streak (
        id                   INTEGER PRIMARY KEY CHECK (id = 1),
        current_streak       INTEGER NOT NULL DEFAULT 0,
        max_streak           INTEGER NOT NULL DEFAULT 0,
        last_completed_date  DATE,
        updated_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,

    # バッチ実行ログ
    """
    CREATE TABLE IF NOT EXISTS batch_run_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        run_date    DATE NOT NULL,
        batch_type  TEXT NOT NULL,
        start_time  DATETIME,
        end_time    DATETIME,
        status      TEXT CHECK(status IN ('running','success','failed'))
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

    logger.info(f"DB 初期化���了: {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
