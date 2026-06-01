"""
バッチ実行ログの共通ヘルパー

全サブシステムの batch_run_log テーブルに対する
開始 / 完了ログの INSERT / UPDATE を統一的に提供する。
"""

import sqlite3
from datetime import datetime, timedelta

from shared.db import insert_one

STALE_RUNNING_MINUTES = 180


def now_str() -> str:
    """現在時刻を ``YYYY-MM-DD HH:MM:SS`` 形式で返す。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def mark_stale_running_batches(
    conn: sqlite3.Connection,
    batch_type: str,
    *,
    stale_after_minutes: int = STALE_RUNNING_MINUTES,
    now: datetime | None = None,
) -> int:
    """同種バッチの古い running レコードを failed に更新する。"""
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(batch_run_log)").fetchall()
    }
    required = {"batch_type", "start_time", "end_time", "status"}
    if not required.issubset(columns):
        return 0

    current = now or datetime.now()
    threshold = current - timedelta(minutes=stale_after_minutes)
    cursor = conn.execute(
        """
        UPDATE batch_run_log
        SET status = 'failed', end_time = ?
        WHERE batch_type = ?
          AND status = 'running'
          AND start_time IS NOT NULL
          AND start_time < ?
        """,
        (
            current.strftime("%Y-%m-%d %H:%M:%S"),
            batch_type,
            threshold.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    return cursor.rowcount


def start_batch_log(
    conn: sqlite3.Connection,
    batch_type: str,
    run_date: str,
    extra: dict | None = None,
) -> int:
    """
    batch_run_log に開始レコードを INSERT し、id を返す。

    Args:
        conn: SQLite 接続
        batch_type: バッチ種別（"morning", "evening_jp" など）
        run_date: 実行日（"YYYY-MM-DD"）
        extra: 追加カラムの初期値（例: {"total_tickers": 0}）
    """
    mark_stale_running_batches(conn, batch_type)
    row: dict = {
        "run_date": run_date,
        "batch_type": batch_type,
        "start_time": now_str(),
        "status": "running",
    }
    if extra:
        row.update(extra)
    return insert_one(conn, "batch_run_log", row)


def finish_batch_log(
    conn: sqlite3.Connection,
    batch_id: int,
    status: str,
    extra: dict | None = None,
) -> None:
    """
    batch_run_log の終了情報を UPDATE する。

    Args:
        conn: SQLite 接続
        batch_id: start_batch_log が返した id
        status: 終了ステータス（"success", "failed" など）
        extra: 追加カラムの更新値（例: {"total_tickers": 10}）
    """
    sets = ["status=?", "end_time=?"]
    params: list = [status, now_str()]
    if extra:
        for key, value in extra.items():
            sets.append(f"{key}=?")
            params.append(value)
    params.append(batch_id)
    conn.execute(
        f"UPDATE batch_run_log SET {', '.join(sets)} WHERE id=?",
        tuple(params),
    )
