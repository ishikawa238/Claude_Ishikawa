"""
Slack 通知モジュール

スコア閾値を超えた論文を Slack Block Kit で通知する。
"""

from datetime import datetime

from shared.slack import divider, section, send_webhook
from src.config import CONFIG
from src.logger import get_logger
from src.translator import translate_keywords, translate_to_ja

logger = get_logger(__name__)


def _build_paper_block(paper: dict) -> list[dict]:
    """1件の論文を Block Kit ブロックリストに変換する。"""
    title = paper.get("title", "(無題)")
    url = paper.get("url", "")
    score = paper.get("score", 0)
    keywords = paper.get("matched_keywords", "")
    abstract = paper.get("abstract", "")

    # キーワードとアブストラクトを日本語に翻訳
    keywords_ja = translate_keywords(keywords) if keywords else ""
    abstract_ja = translate_to_ja(abstract) if abstract else ""

    # 翻訳後のアブストラクトを200文字に切り詰め
    if len(abstract_ja) > 200:
        abstract_ja = abstract_ja[:200] + "..."

    title_line = f"*<{url}|{title}>*" if url else f"*{title}*"
    detail = f"Score: *{score}* | Keywords: {keywords_ja}"

    blocks = [
        section(title_line),
        section(detail),
    ]
    if abstract_ja:
        blocks.append(section(f">{abstract_ja}"))

    return blocks


def notify_papers(papers: list[dict]) -> bool:
    """
    論文リストを Slack に通知する。

    Args:
        papers: スコア付き論文リスト（score_threshold 以上に絞り込み済みを想定）

    Returns:
        通知成功なら True
    """
    max_notify = CONFIG.get("scoring", {}).get("max_notify", 10)
    webhook_url = CONFIG.get("slack", {}).get("webhook_url", "")

    to_notify = papers[:max_notify]
    if not to_notify:
        logger.info("通知対象の論文はありません")
        return 0

    today = datetime.now().strftime("%Y-%m-%d")
    blocks: list[dict] = [
        section(f":newspaper: *新着論文レポート ({today})* — {len(to_notify)} 件"),
        divider(),
    ]

    for paper in to_notify:
        blocks.extend(_build_paper_block(paper))
        blocks.append(divider())

    payload = {"blocks": blocks}
    ok = send_webhook(webhook_url, payload, logger=logger)

    if ok:
        logger.info(f"Slack 通知完了: {len(to_notify)} 件")
    else:
        logger.error("Slack 通知に失敗しました")

    return ok
