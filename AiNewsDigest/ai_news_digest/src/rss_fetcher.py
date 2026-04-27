"""
RSS フィード取得モジュール

config.json の rss.feeds から各フィードを取得し、記事リストを返す。
"""

import time
from datetime import datetime, timezone

import feedparser

from src.config import CONFIG
from src.logger import get_logger

logger = get_logger(__name__)

_FEEDS = CONFIG.get("rss", {}).get("feeds", [])
_TIMEOUT = int(CONFIG.get("rss", {}).get("request_timeout_sec", 15))
_INTERVAL = float(CONFIG.get("rss", {}).get("request_interval_sec", 2.0))


def fetch_all_feeds() -> list[dict]:
    """
    設定済みの全 RSS フィードから記事を取得する。

    Returns:
        記事辞書のリスト。各辞書のキー:
        - title: 記事タイトル
        - url: 記事 URL
        - summary: 記事要約（RSS の description）
        - source: フィード名
        - published_at: 公開日時（ISO 8601 文字列、不明なら None）
    """
    all_articles: list[dict] = []

    for i, feed_cfg in enumerate(_FEEDS):
        name = feed_cfg.get("name", f"Feed-{i}")
        url = feed_cfg.get("url", "")
        if not url:
            logger.warning(f"フィード '{name}' に URL が未設定です。スキップします。")
            continue

        try:
            articles = _fetch_one_feed(name, url)
            all_articles.extend(articles)
            logger.info(f"{name}: {len(articles)} 件取得")
        except Exception as e:
            logger.error(f"{name}: 取得失敗 — {e}", exc_info=True)

        # レート制限（最後のフィードではスリープしない）
        if i < len(_FEEDS) - 1:
            time.sleep(_INTERVAL)

    logger.info(f"全フィード合計: {len(all_articles)} 件")
    return all_articles


def _fetch_one_feed(name: str, url: str) -> list[dict]:
    """1 つの RSS フィードを取得して記事リストを返す。"""
    feed = feedparser.parse(url, request_headers={"User-Agent": "AiNewsDigest/1.0"})

    if feed.bozo and not feed.entries:
        raise RuntimeError(f"フィードのパースに失敗: {feed.bozo_exception}")

    articles = []
    for entry in feed.entries:
        article = {
            "title": entry.get("title", "").strip(),
            "url": entry.get("link", "").strip(),
            "summary": _extract_summary(entry),
            "source": name,
            "published_at": _parse_published(entry),
        }
        if article["title"] and article["url"]:
            articles.append(article)

    return articles


def _extract_summary(entry) -> str:
    """RSS エントリから要約テキストを抽出する。"""
    summary = entry.get("summary", "") or entry.get("description", "")
    # HTML タグの簡易除去
    if "<" in summary:
        import re
        summary = re.sub(r"<[^>]+>", "", summary)
    return summary.strip()[:500]


def _parse_published(entry) -> str | None:
    """RSS エントリの公開日時を ISO 8601 文字列に変換する。"""
    published_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if published_parsed:
        try:
            dt = datetime(*published_parsed[:6], tzinfo=timezone.utc)
            return dt.isoformat()
        except (TypeError, ValueError):
            pass
    return None
