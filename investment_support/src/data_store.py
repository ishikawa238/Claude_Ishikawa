"""
データベース接続・基本操作モジュール

役割:
- SQLite 接続の管理（コンテキストマネージャ形式）
- UPSERT / INSERT / SELECT のユーティリティ提供
- DBバックアップ
"""

import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from src.config import CONFIG
from src.logger import get_logger

logger = get_logger(__name__)

_DB_PATH = Path(CONFIG["database"]["db_path"])
_BACKUP_DIR = Path(CONFIG["database"]["backup_dir"])
_BACKUP_RETENTION_DAYS = int(CONFIG["database"].get("backup_retention_days", 7))


@contextmanager
def get_connection():
    """
    SQLite 接続をコンテキストマネージャとして提供する。
    with get_connection() as conn: の形で使う。
    例外が発生した場合は自動的にロールバックされる。
    """
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row  # カラム名でアクセス可能にする
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # 並行読み書きの安定性向上
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert(conn: sqlite3.Connection, table: str, rows: list[dict]) -> int:
    """
    INSERT OR REPLACE による UPSERT を実行する。
    rows: 辞書のリスト（全行が同じキー構成であること）
    返り値: 処理した行数
    """
    if not rows:
        return 0

    columns = list(rows[0].keys())
    placeholders = ", ".join(["?"] * len(columns))
    col_names = ", ".join(columns)
    sql = f"INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})"

    data = [tuple(row[c] for c in columns) for row in rows]
    conn.executemany(sql, data)
    return len(rows)


def insert_one(conn: sqlite3.Connection, table: str, row: dict) -> int:
    """
    1行だけ INSERT する。返り値は lastrowid。
    """
    columns = list(row.keys())
    placeholders = ", ".join(["?"] * len(columns))
    col_names = ", ".join(columns)
    sql = f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})"
    cursor = conn.execute(sql, tuple(row.values()))
    return cursor.lastrowid


def fetch_all(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """SELECT 結果を全件返す。"""
    return conn.execute(sql, params).fetchall()


def fetch_one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    """SELECT 結果の先頭1件を返す。"""
    return conn.execute(sql, params).fetchone()


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
