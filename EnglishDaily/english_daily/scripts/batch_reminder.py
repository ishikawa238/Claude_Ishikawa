"""
リマインドバッチスクリプト — EnglishDaily

実行方法（english_daily/ から）:
    python -m scripts.batch_reminder

処理フ���ー:
    1. 今日の朝メッセージを DB から取得
    2. Slack リアクションで完了状態を確認
    3. 未完了タスクがあればリマインド通知を送信

スケジュール: 17:30 / 19:30 / 21:30 / 23:30
現在時刻からリマインドスロット（トーンの強さ）を自動判定する。
"""

import sys
from datetime import date, datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.config import CONFIG
from src.curriculum import REACTION_NAMES, get_tasks
from src.data_store import fetch_one, get_connection, log_error
from src.logger import get_logger
from src.notifier import send_reminder
from src.slack_client import SlackClient

logger = get_logger(__name__)
_TODAY = date.today()
_BATCH_TYPE = "reminder"


def main() -> int:
    try:
        logger.info("=" * 60)
        logger.info(f"[EnglishDaily リマインド] 開始: {_TODAY}")
        logger.info("=" * 60)

        bot_token = CONFIG["slack"]["bot_token"]
        channel = CONFIG["slack"]["channel"]
        client = SlackClient(bot_token, logger=logger)

        # STEP 1: 今日のメッセージ情報を取得
        logger.info("STEP 1: 今日のメッセージ情報を取得")
        with get_connection() as conn:
            msg = fetch_one(
                conn,
                "SELECT channel_id, message_ts, task_count "
                "FROM daily_message WHERE date = ?",
                (_TODAY.strftime("%Y-%m-%d"),),
            )

        if not msg:
            logger.warning("今日の朝メッセージが見つかりません — スキップ")
            return 0

        # STEP 2: リアクション確認
        logger.info("STEP 2: リアクション確認")
        reactions = client.get_reactions(msg["channel_id"], msg["message_ts"])
        tasks = get_tasks(_TODAY)

        uncompleted = []
        for i, task in enumerate(tasks):
            reaction_name = REACTION_NAMES[i] if i < len(REACTION_NAMES) else None
            if reaction_name and reaction_name not in reactions:
                uncompleted.append((i, task))

        if not uncompleted:
            logger.info("全タスク完了済み — リマインド不要 🎉")
            return 0

        logger.info(f"未完了タスク: {len(uncompleted)} 件")

        # STEP 3: リマインド送信
        logger.info("STEP 3: リ���インド送信")
        slot_index = _detect_slot_index()
        send_reminder(client, channel, uncompleted, slot_index, logger)

        logger.info("[EnglishDaily リマイン���] 完了")
        return 0

    except Exception as e:
        logger.error(f"[リマインド] 致命的エラー: {e}", exc_info=True)
        log_error(_BATCH_TYPE, "batch_reminder", type(e).__name__, str(e), is_fatal=True)
        return 2


def _detect_slot_index() -> int:
    """現在時刻からリマインドスロット番号を返す。"""
    hour = datetime.now().hour
    if hour < 18:
        return 0  # 17:30 枠
    if hour < 20:
        return 1  # 19:30 枠
    if hour < 22:
        return 2  # 21:30 枠
    return 3      # 23:30 枠


if __name__ == "__main__":
    sys.exit(main())
