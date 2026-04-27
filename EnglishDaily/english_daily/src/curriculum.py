"""
カリキュラム管理モジュール

config.json の curriculum セクションから、曜日に応じたタスク一覧を返す。
"""

from datetime import date

from src.config import CONFIG

# config の曜日キー → Python weekday() の対応
_WEEKDAY_KEYS = [
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
]

# タスク番号に対応する Slack リアクション名
REACTION_NAMES = ["one", "two", "three", "four"]

# タスク番号に対応する表示用絵文字
REACTION_EMOJIS = [":one:", ":two:", ":three:", ":four:"]


def get_tasks(target_date: date | None = None) -> list[str]:
    """
    指定日（デフォルト: 今日）のタスク一覧を返す。

    Returns:
        タスク文字列のリスト（例: ["単語: 新しい単語を10個覚える", "リスニング: ..."]）
    """
    if target_date is None:
        target_date = date.today()

    weekday_key = _WEEKDAY_KEYS[target_date.weekday()]
    curriculum = CONFIG.get("curriculum", {})
    return curriculum.get(weekday_key, [])
