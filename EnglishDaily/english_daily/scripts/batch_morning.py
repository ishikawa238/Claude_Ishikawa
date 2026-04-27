"""
朝バッチスクリプト — EnglishDaily

実行方法（english_daily/ から）:
    python -m scripts.batch_morning

処理フロー:
    1. 前日のタスク完了状態をチェックし、ストリーク更新
    2. 今日のカリキュラムを取得
    3. Slack に朝の配信を送信
    4. メッセージ情報を DB に保存
"""

import sys
from datetime import date, datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from shared.batch import start_batch_log, finish_batch_log
from src.config import CONFIG
from src.curriculum import get_tasks
from src.data_store import get_connection, log_error
from src.logger import get_logger
from src.notifier import send_morning
from src.slack_client import SlackClient
from src.streak import update_streak_for_yesterday

logger = get_logger(__name__)
_TODAY = date.today()
_BATCH_TYPE = "morning"


def main() -> int:
    with get_connection() as conn:
        batch_id = start_batch_log(conn, _BATCH_TYPE, _TODAY.strftime("%Y-%m-%d"))

    try:
        logger.info("=" * 60)
        logger.info(f"[EnglishDaily 朝バッチ] 開始: {_TODAY}")
        logger.info("=" * 60)

        bot_token = CONFIG["slack"]["bot_token"]
        channel = CONFIG["slack"]["channel"]
        client = SlackClient(bot_token, logger=logger)

        # STEP 1: ストリーク更新（前日のリアクションを確認）
        logger.info("STEP 1: ストリーク更新")
        current_streak = update_streak_for_yesterday(client, logger)

        # STEP 2: 今日のタスク取得
        logger.info("STEP 2: カリキュラム取得")
        tasks = get_tasks(_TODAY)
        logger.info(f"今日のタスク: {len(tasks)} 件")

        if not tasks:
            logger.warning("今日のタスクが設定されていません")
            with get_connection() as conn:
                finish_batch_log(conn, batch_id, "success")
            return 0

        # STEP 3: Slack 配信
        logger.info("STEP 3: Slack 配信")
        result = send_morning(client, channel, _TODAY, tasks, current_streak, logger)

        if not result:
            logger.error("Slack 配信に失敗しました")
            with get_connection() as conn:
                finish_batch_log(conn, batch_id, "failed")
            return 1

        # STEP 4: メッセージ情報を DB に保存
        logger.info("STEP 4: メッセージ情報を DB に保存")
        with get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO daily_message "
                "(date, channel_id, message_ts, task_count) "
                "VALUES (?, ?, ?, ?)",
                (
                    _TODAY.strftime("%Y-%m-%d"),
                    result["channel"],
                    result["ts"],
                    len(tasks),
                ),
            )

        with get_connection() as conn:
            finish_batch_log(conn, batch_id, "success")
        logger.info("[EnglishDaily 朝バッチ] 完了: success")
        return 0

    except Exception as e:
        logger.error(f"[朝バッチ] 致命的エラー: {e}", exc_info=True)
        log_error(_BATCH_TYPE, "batch_morning", type(e).__name__, str(e), is_fatal=True)
        with get_connection() as conn:
            finish_batch_log(conn, batch_id, "failed")
        return 2



if __name__ == "__main__":
    sys.exit(main())
