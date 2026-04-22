"""
汎用 SQLite ユーティリティ（共有版）

コンテキストマネージャ形式の接続管理と、基本的な CRUD ヘルパーを提供する。
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def get_connection(db_path: Path | str):
    """
    SQLite 接続をコンテキストマネージャとして提供する。
    with get_connection(db_path) as conn: の形で使う。
    例外が発生した場合は自動的にロールバックされる。
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
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


def fetch_all(
    conn: sqlite3.Connection, sql: str, params: tuple = ()
) -> list[sqlite3.Row]:
    """SELECT 結果を全件返す。"""
    return conn.execute(sql, params).fetchall()


def fetch_one(
    conn: sqlite3.Connection, sql: str, params: tuple = ()
) -> sqlite3.Row | None:
    """SELECT 結果の先頭1件を返す。"""
    return conn.execute(sql, params).fetchone()
