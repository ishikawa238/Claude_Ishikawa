"""
Slack 通知（見て楽しく、ひと目で分かる構成）。
"""

from shared import slack
from shared.config import get_config
from shared.logger import get_logger
from src.selector import NEW

WEB_URL = "http://127.0.0.1:5210/"


def _logger():
    return get_logger(__name__)


def _webhook() -> str:
    return get_config().get("slack", {}).get("webhook_url", "")


def fmt_minutes(seconds: int | None) -> str:
    return f"{round((seconds or 0) / 60)}分"


def _lines(queue: list[tuple[dict, str]], source: str) -> list[str]:
    return [
        f"• *{e['show']}* — {e['title'][:60]} （{fmt_minutes(e.get('duration_sec'))}）"
        for e, src in queue
        if src == source
    ]


def build_success_blocks(queue: list[tuple[dict, str]], stats: dict, label: str) -> list[dict]:
    total = sum(e.get("duration_sec") or 0 for e, _ in queue)
    new_lines = _lines(queue, NEW)
    shuffle_lines = _lines(queue, "shuffle")
    blocks = [
        slack.section(
            f":radio: *おはようございます！ {label} はじまります* :sunrise:\n"
            f"今日は *{len(queue)}本 / {fmt_minutes(total)}* をお届けします :headphones:"
        ),
        slack.divider(),
    ]
    if new_lines:
        blocks.append(slack.section(":new: *今日の新着*\n" + "\n".join(new_lines)))
    else:
        blocks.append(slack.section(":zzz: 今日は新着がありませんでした。ライブラリからお届けします"))
    if shuffle_lines:
        blocks.append(slack.section(":twisted_rightwards_arrows: *ライブラリからシャッフル*\n" + "\n".join(shuffle_lines)))
    size_gb = stats.get("size_bytes", 0) / 1_073_741_824
    blocks += [
        slack.divider(),
        slack.section(
            f":books: ライブラリ: *{stats.get('count', 0)}本*"
            f"（未再生 {stats.get('unplayed', 0)}本 / {size_gb:.1f}GB） | <{WEB_URL}|管理画面>"
        ),
    ]
    return blocks


def build_failure_blocks(error: str, hint: str) -> list[dict]:
    return [
        slack.section(
            f":warning: *英語ラジオを再生できませんでした*\n\n"
            f"*原因:* {error}\n"
            f":bulb: *対処:* {hint}\n"
            f"<{WEB_URL}|管理画面で状況を見る>"
        ),
    ]


def hint_for(error: Exception) -> str:
    text = str(error)
    if "VLC" in text:
        return "VLC をインストールしてください（https://www.videolan.org/）"
    if "ライブラリ" in text:
        return "ネット接続を確認し、管理画面の「作り直して再生」で再実行してください"
    return "ログ（english_radio/logs/）を確認してください"


def send_success(queue: list[tuple[dict, str]], stats: dict, label: str, dry_run: bool = False) -> None:
    payload = {"blocks": build_success_blocks(queue, stats, label)}
    if dry_run:
        slack.print_blocks(payload)
        return
    slack.send_webhook(_webhook(), payload, logger=_logger())


def send_failure(error: Exception, dry_run: bool = False) -> None:
    payload = {"blocks": build_failure_blocks(str(error), hint_for(error))}
    if dry_run:
        slack.print_blocks(payload)
        return
    slack.send_webhook(_webhook(), payload, logger=_logger())
