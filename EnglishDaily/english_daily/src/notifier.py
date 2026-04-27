"""
Slack 通知モジュール（EnglishDaily 用）

朝のタスク配信とリマインド通知を Block Kit 形式で送信する。
"""

from datetime import date

from shared.slack import divider, section
from src.curriculum import REACTION_EMOJIS

_WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]

# リマインド時のトーン（時間帯ごとに段階的に変化）
_REMINDER_TONES = [
    {  # 17:30 — やさしめ
        "header": "⏰ まだ終わってないタスクがあるよ！",
        "footer": "今日もあと少し、がんばろう💪",
    },
    {  # 19:30 — 普通
        "header": "📣 リマインド！ 残りタスクを片付けよう",
        "footer": "せっかくのストリーク、ここで途切れるのはもったいない！",
    },
    {  # 21:30 — 強め
        "header": "🚨 今日のタスクがまだ残ってるよ！",
        "footer": "5分でもいいからやろう！",
    },
    {  # 23:30 — ラストコール
        "header": "🌙 ラストチャンス！ 寝る前にサクッと終わらせよう",
        "footer": "明日の自分が感謝するよ✨",
    },
]


def build_morning_blocks(
    target_date: date,
    tasks: list[str],
    current_streak: int,
) -> list[dict]:
    """朝の配信メッセージの Block Kit ブロックを組み立てる。"""
    weekday_ja = _WEEKDAY_JA[target_date.weekday()]
    date_str = target_date.strftime("%Y-%m-%d")

    streak_text = f"  🔥 {current_streak}日連続!" if current_streak > 0 else ""
    header = f"📚 *今日の英語学習 ({date_str} {weekday_ja})*{streak_text}"

    blocks = [section(header), divider()]

    task_lines = []
    for i, task in enumerate(tasks):
        emoji = REACTION_EMOJIS[i] if i < len(REACTION_EMOJIS) else f"*{i + 1}.*"
        task_lines.append(f"{emoji} {task}")
    blocks.append(section("\n".join(task_lines)))

    blocks.append(divider())
    blocks.append(section("✅ 終わったタスクの番号リアクションを押してね！"))

    return blocks


def build_reminder_blocks(
    uncompleted: list[tuple[int, str]],
    slot_index: int,
) -> list[dict]:
    """
    リマインドメッセージの Block Kit ブロックを組み立てる。

    Args:
        uncompleted: [(タスク番号(0始まり), タスク文字列), ...]
        slot_index: リマインドスロット (0=17:30, 1=19:30, 2=21:30, 3=23:30)
    """
    tone = _REMINDER_TONES[min(slot_index, len(_REMINDER_TONES) - 1)]

    blocks = [section(f"*{tone['header']}*"), divider()]

    task_lines = []
    for idx, task in uncompleted:
        emoji = REACTION_EMOJIS[idx] if idx < len(REACTION_EMOJIS) else f"*{idx + 1}.*"
        task_lines.append(f"{emoji} {task}")
    blocks.append(section("\n".join(task_lines)))

    blocks.append(divider())
    blocks.append(section(tone["footer"]))

    return blocks


def send_morning(slack_client, channel, target_date, tasks, current_streak, logger):
    """
    朝のタスク配信を送信する。

    Returns:
        成功時 {"channel": ..., "ts": ...}, 失敗時 None
    """
    blocks = build_morning_blocks(target_date, tasks, current_streak)
    result = slack_client.post_message(channel, blocks, text="今日の英語学習")
    if result:
        logger.info("朝の配信を送信しました")
    else:
        logger.error("朝の配信の送信に失敗しました")
    return result


def send_reminder(slack_client, channel, uncompleted, slot_index, logger):
    """
    リマインド通知を送信する。

    Returns:
        成功時 {"channel": ..., "ts": ...}, 失敗時 None
    """
    blocks = build_reminder_blocks(uncompleted, slot_index)
    result = slack_client.post_message(channel, blocks, text="英語学習リマインド")
    if result:
        logger.info(f"リマインド (slot {slot_index}) を送信しました")
    else:
        logger.error("リマインド送信に失敗しました")
    return result
