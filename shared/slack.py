"""
Slack Webhook 送信 + Block Kit ヘルパー（共有版）

低レベルの Slack Incoming Webhook 送信と、
Block Kit の基本ブロック生成関数を提供する。
"""

import io
import json
import sys
import time

import requests

_MAX_RETRIES = 3
_RETRY_BASE_SEC = 2  # 指数バックオフの基底（2, 4, 8秒）


def send_webhook(
    webhook_url: str,
    payload: dict,
    *,
    logger=None,
    timeout: int = 15,
) -> bool:
    """
    Slack Incoming Webhook に payload を POST する。
    一時的な障害時は最大 _MAX_RETRIES 回リトライする（指数バックオフ）。
    返り値: 成功なら True

    webhook_url が空文字列または "XXXXX" を含む場合はスキップし、
    コンソールにブロック内容を出力する。
    """
    if not webhook_url or "XXXXX" in webhook_url:
        if logger:
            logger.warning("Slack Webhook URL が未設定です。コンソールに出力します。")
        print_blocks(payload)
        return True

    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = requests.post(
                webhook_url,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )
            if resp.status_code == 200:
                if logger:
                    logger.info("Slack 送信成功")
                return True
            # 5xx はリトライ対象
            if resp.status_code >= 500 and attempt < _MAX_RETRIES:
                wait = _RETRY_BASE_SEC ** attempt
                if logger:
                    logger.warning(
                        f"Slack 送信失敗 (HTTP {resp.status_code})、"
                        f"{wait}秒後にリトライします（{attempt}/{_MAX_RETRIES}）"
                    )
                time.sleep(wait)
                continue
            # 4xx やリトライ回数超過
            if logger:
                logger.error(f"Slack 送信失敗: HTTP {resp.status_code} / {resp.text}")
            return False
        except Exception as e:
            last_error = e
            if attempt < _MAX_RETRIES:
                wait = _RETRY_BASE_SEC ** attempt
                if logger:
                    logger.warning(
                        f"Slack 送信エラー ({e})、"
                        f"{wait}秒後にリトライします（{attempt}/{_MAX_RETRIES}）"
                    )
                time.sleep(wait)
            else:
                if logger:
                    logger.error(f"Slack 送信エラー（リトライ上限到達）: {last_error}")
                return False
    return False


def section(text: str) -> dict:
    """Block Kit の section ブロックを返す。"""
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def divider() -> dict:
    """Block Kit の divider ブロックを返す。"""
    return {"type": "divider"}


def print_blocks(payload: dict) -> None:
    """Webhook 未設定時にブロック内容をコンソールに出力する。"""
    out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    out.write("\n" + "=" * 60 + "\n")
    for block in payload.get("blocks", []):
        if block.get("type") == "section":
            text = block.get("text", {}).get("text", "")
            out.write(text + "\n")
        elif block.get("type") == "divider":
            out.write("-" * 40 + "\n")
    out.write("=" * 60 + "\n")
    out.flush()
