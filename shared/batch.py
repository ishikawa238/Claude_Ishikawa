"""
バッチ実行ログの共通ヘルパー

全サブシステムの batch_run_log テーブルに対する
開始 / 完了ログの INSERT / UPDATE を統一的に提供する。
"""

import sqlite3
from datetime import datetime

from shared.db import insert_one


def now_str() -> str:
    """現在時刻を ``YYYY-MM-DD HH:MM:SS`` 形式で返す。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
