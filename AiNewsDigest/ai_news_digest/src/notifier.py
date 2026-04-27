"""
Slack 通知モジュール（AiNewsDigest 用）

新着ニュースを Block Kit 形式で Slack に送信する。
新着 0 件の場合は通知をスキップしログのみ出力する。
"""

from datetime import date

from shared.slack import (
    divider as _divider,
    section as _section,
    send_webhook,
)

from src.config import CONFIG
from src.logger import get_logger

logger = get_logger(__name__)

_WEBHOOK_URL = CONFIG.get("slack", {}).get("webhook_url", "")
_MAX_NOTIFY = int(CONFIG.get("filter", {}).get("max_notify_total", 20))


def send_news_digest(
    target_date: date,
    new_articles: list[dict],
    stats: dict,
) -> bool:
    """
    新着ニュースを Slack に通知する。

    Args:
        target_date: 対象日
        new_articles: フィルタ済み新着記事リスト
        stats: バッチ統計情報
            - total_fetched: 全フィード取得件数
            - feed_counts: フィードごとの取得件数 dict

    Returns:
        送信成功なら True（新着 0 件でスキップした場合も True）
    """
    if not new_articles:
        logger.info("新着ニュース 0 件 — Slack 通知をスキップします。")
        return True

    date_str = target_date.strftime("%Y-%m-%d")
    blocks = []

    # ヘッダー
    blocks.append(_section(f"*AI ニュースダイジェスト | {date_str}*"))
    blocks.append(_divider())

    # サマリー
    total = stats.get("total_fetched", 0)
    blocks.append(_section(
        f"新着: *{len(new_articles)}件* / 取得総数: {total}件"
    ))
    blocks.append(_divider())

    # ソース別にグループ化
    by_source: dict[str, list[dict]] = {}
    for article in new_articles:
        source = article.get("source", "Unknown")
        by_source.setdefault(source, []).append(article)

    for source, articles in by_source.items():
        blocks.append(_section(f"*{source}（{len(articles)}件）*"))
        show = articles[:_MAX_NOTIFY]
        for article in show:
            blocks.append(_article_block(article))
        rest = len(articles) - len(show)
        if rest > 0:
            blocks.append(_section(f"_…他 {rest} 件_"))
        blocks.append(_divider())

    payload = {"blocks": blocks}
    success = send_webhook(_WEBHOOK_URL, payload, logger=logger)
    return success


def _article_block(article: dict) -> dict:
    """1 記事分の Block Kit セクションを生成する。"""
    title = article.get("title", "（タイトルなし）")
    url = article.get("url", "")
    summary = article.get("summary", "")

    parts = [f"*{title}*"]

    if summary:
        # 要約は最大 150 文字に切り詰め
        short = summary[:150] + ("…" if len(summary) > 150 else "")
        parts.append(f"  {short}")

    if url:
        parts.append(f"  <{url}|記事を読む>")

    return _section("\n".join(parts))
