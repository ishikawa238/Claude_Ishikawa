"""
データベース接続・基本操作モジュール

役割:
- SQLite 接続の管理（コンテキストマネージャ形式）
- UPSERT / INSERT / SELECT のユーティリティ提供
- DBバックアップ

汎用 CRUD ヘルパーは shared.db から提供される。
本モジュールはプロジェクト固有の接続設定と運用機能を追加する。
"""

import shutil
from contextlib import contextmanager
from datetime import datetime, timedelta
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
_BACKUP_DIR = Path(CONFIG["database"]["backup_dir"])
_BACKUP_RETENTION_DAYS = int(CONFIG["database"].get("backup_retention_days", 7))


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
    ticker: str | None = None,
    is_fatal: bool = False,
) -> None:
    """
    エラーを error_log テーブルに記録する。
    DB 接続エラー時はファイルログのみに記録する。
    """
    try:
        with get_connection() as conn:
            insert_one(
                conn,
                "error_log",
                {
                    "error_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "batch_type": batch_type,
                    "module": module,
                    "ticker": ticker,
                    "error_type": error_type,
                    "error_message": str(error_message)[:2000],
                    "is_fatal": 1 if is_fatal else 0,
                },
            )
    except Exception as e:
        logger.error(f"error_log への書き込みに失敗しました: {e}")


def backup_database() -> bool:
    """
    DB ファイルを backup_dir にタイムスタンプ付きでコピーする。
    古いバックアップ（retention_days 超過分）を削除する。
    返り値: 成功なら True、失敗なら False
    """
    if not _DB_PATH.exists():
        logger.warning("バックアップ対象の DB ファイルが存在しません。")
        return False

    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = _BACKUP_DIR / f"investment_{timestamp}.db"

    try:
        shutil.copy2(_DB_PATH, backup_path)
        logger.info(f"DBバックアップ完了: {backup_path}")
    except Exception as e:
        logger.error(f"DBバックアップ失敗: {e}")
        return False

    # 古いバックアップの削除
    cutoff = datetime.now() - timedelta(days=_BACKUP_RETENTION_DAYS)
    for old_file in _BACKUP_DIR.glob("investment_*.db"):
        try:
            mtime = datetime.fromtimestamp(old_file.stat().st_mtime)
            if mtime < cutoff:
                old_file.unlink()
                logger.info(f"古いバックアップを削除: {old_file.name}")
        except Exception as e:
            logger.warning(f"バックアップ削除スキップ ({old_file.name}): {e}")

    return True
