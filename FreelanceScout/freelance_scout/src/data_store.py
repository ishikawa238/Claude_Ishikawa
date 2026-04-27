"""
データベース接続・基本操作モジュール（FreelanceScout 用）

プロジェクト固有の DB パスで接続を提供し、エラーログ機能を追加する。
汎用 CRUD ヘルパーは shared.db から再エクスポートする。
"""

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from shared.db import (  # noqa: F401 — 再エクスポート
    fetch_all,
    fetch_one,
    insert_one,
    upsert,
)
from shared.db import get_connection as _shared_get_connection
from src.config import CONFIG
from src.logger import get_logger

logger = get_logger(__name__)

_DB_PATH = Path(CONFIG["database"]["db_path"])


@contextmanager
def get_connection():
    """
    プロジェクト設定の DB パスを使って SQLite 接続を提供する。
    with get_connection() as conn: の形で使う。
    """
    with _shared_get_connection(_DB_PATH) as conn:
        yield conn


def log_error(
    batch_type: str | None,
    module: str,
    error_type: str,
    error_message: str,
    is_fatal: bool = False,
) -> None:
    """エラーを error_log テーブルに記録する。"""
    try:
        with get_connection() as conn:
            insert_one(
                conn,
                "error_log",
                {
                    "error_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "batch_type": batch_type,
                    "module": module,
                    "error_type": error_type,
                    "error_message": str(error_message)[:2000],
                    "is_fatal": 1 if is_fatal else 0,
                },
            )
    except Exception as e:
        logger.error(f"error_log への書き込みに失敗しました: {e}")
