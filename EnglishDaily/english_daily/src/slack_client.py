"""
Slack Bot Token API クライアント（EnglishDaily 用）

Incoming Webhook ではなく Bot Token を使い、
メッセージ投稿とリアクション読み取りを行う。
"""

import json

import requests

from shared.slack import divider, print_blocks, section  # noqa: F401 — 再エクスポート

_BASE_URL = "https://slack.com/api"


class SlackClient:
    """Slack Web API の薄いラッパー。"""

    def __init__(self, bot_token: str, *, logger=None):
        self.bot_token = bot_token
        self.logger = logger
        self._headers = {
            "Authorization": f"Bearer {bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def post_message(
        self,
        channel: str,
        blocks: list[dict],
        text: str = "",
    ) -> dict | None:
        """
        チャンネルにメッセージを投稿する。

        Returns:
            成功時 {"channel": "<channel_id>", "ts": "<message_ts>"}
            失敗時 None
        """
        if self._is_placeholder():
            self._fallback_print(blocks)
            return {"channel": "console", "ts": "0"}

        data = self._post("chat.postMessage", {
            "channel": channel,
            "blocks": blocks,
            "text": text or "EnglishDaily",
        })
        if data:
            return {"channel": data["channel"], "ts": data["ts"]}
        return None

    def get_reactions(self, channel: str, timestamp: str) -> list[str]:
        """
        メッセージに付いたリアクション名の一覧を返す。

        Returns:
            リアクション名のリスト（例: ["one", "two"]）
        """
        if self._is_placeholder():
            return []

        resp = self._get("reactions.get", {
            "channel": channel,
            "timestamp": timestamp,
        })
        if resp:
            reactions = resp.get("message", {}).get("reactions", [])
            return [r["name"] for r in reactions]
        return []

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _post(self, method: str, payload: dict) -> dict | None:
        try:
            resp = requests.post(
                f"{_BASE_URL}/{method}",
                headers=self._headers,
                data=json.dumps(payload),
                timeout=15,
            )
            body = resp.json()
            if body.get("ok"):
                if self.logger:
                    self.logger.info(f"Slack API {method} 成功")
                return body
            if self.logger:
                self.logger.error(
                    f"Slack API {method} 失敗: {body.get('error', body)}"
                )
            return None
        except Exception as e:
            if self.logger:
                self.logger.error(f"Slack API {method} エラー: {e}")
            return None

    def _get(self, method: str, params: dict) -> dict | None:
        try:
            resp = requests.get(
                f"{_BASE_URL}/{method}",
                headers={"Authorization": self._headers["Authorization"]},
                params=params,
                timeout=15,
            )
            body = resp.json()
            if body.get("ok"):
                return body
            if self.logger:
                self.logger.error(
                    f"Slack API {method} 失敗: {body.get('error', body)}"
                )
            return None
        except Exception as e:
            if self.logger:
                self.logger.error(f"Slack API {method} エラー: {e}")
            return None

    def _is_placeholder(self) -> bool:
        return not self.bot_token or "XXXXX" in self.bot_token

    def _fallback_print(self, blocks: list[dict]) -> None:
        if self.logger:
            self.logger.warning(
                "Slack Bot Token が未設定です。コンソールに出力します。"
            )
        print_blocks({"blocks": blocks})
