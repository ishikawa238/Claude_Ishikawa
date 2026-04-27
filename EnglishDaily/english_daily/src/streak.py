"""
ストリーク（連続学習日数）管理モジュール

毎朝のバッチで前日の完了状態をチェックし、ストリークを更新する。
"""

from datetime import date, timedelta

from src.curriculum import REACTION_NAMES
from src.data_store import fetch_one, get_connection


def get_streak() -> dict:
    """現在のストリーク情報を返す。"""
    with get_connection() as conn:
        row = fetch_one(conn, "SELECT * FROM streak WHERE id = 1")
    if row:
        return dict(row)
    return {
        "current_streak": 0,
        "max_streak": 0,
        "last_completed_date": None,
    }


def update_streak_for_yesterday(slack_client, logger) -> int:
    """
    前日のタスク完了状態をリアクションで確認し、ストリークを更新する。

    Returns:
        更新後の current_streak
    """
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")

    with get_connection() as conn:
        msg = fetch_one(
            conn,
            "SELECT channel_id, message_ts, task_count "
            "FROM daily_message WHERE date = ?",
            (yesterday,),
        )

        if not msg:
            logger.info("前日のメッセージなし — ストリーク変更なし")
            row = fetch_one(conn, "SELECT current_streak FROM streak WHERE id = 1")
            return row["current_streak"] if row else 0

        # Slack リアクションを確認
        reactions = slack_client.get_reactions(msg["channel_id"], msg["message_ts"])
        expected = REACTION_NAMES[: msg["task_count"]]
        completed = sum(1 for name in expected if name in reactions)
        all_done = completed == msg["task_count"]

        # 現在のストリーク取得
        current = fetch_one(conn, "SELECT * FROM streak WHERE id = 1")
        old_streak = current["current_streak"] if current else 0
        old_max = current["max_streak"] if current else 0
        old_last = current["last_completed_date"] if current else None

        if all_done:
            new_streak = old_streak + 1
            new_max = max(new_streak, old_max)
            new_last = yesterday
            logger.info(
                f"前日タスク全完了! ストリーク: {old_streak} → {new_streak}"
            )
        else:
            new_streak = 0
            new_max = old_max
            new_last = old_last
            logger.info(
                f"前日タスク未完了 ({completed}/{msg['task_count']}). "
                f"ストリーク: {old_streak} → 0"
            )

        conn.execute(
            "INSERT OR REPLACE INTO streak "
            "(id, current_streak, max_streak, last_completed_date, updated_at) "
            "VALUES (1, ?, ?, ?, CURRENT_TIMESTAMP)",
            (new_streak, new_max, new_last),
        )
        return new_streak
