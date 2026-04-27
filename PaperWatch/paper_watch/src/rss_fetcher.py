"""
arXiv RSS フィード取得モジュール

指定カテゴリの新着論文を RSS フィードから取得し、
論文情報を辞書リストとして返す。
"""

import re
import time

import feedparser

from src.config import CONFIG
from src.logger import get_logger

logger = get_logger(__name__)

_ARXIV_RSS_URL = "http://rss.arxiv.org/rss/{category}"
_ARXIV_ID_PATTERN = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?$")


def _parse_entry(entry: dict, category: str) -> dict | None:
    """feedparser のエントリ1件を論文 dict に変換する。"""
    link = entry.get("link", "")

    # arxiv_id を URL 末尾から抽出
    m = _ARXIV_ID_PATTERN.search(link)
    if not m:
        logger.debug(f"arxiv_id を抽出できません: {link}")
        return None

    arxiv_id = m.group(1)
    title = entry.get("title", "").strip()
    abstract = entry.get("summary", "").strip()
    authors = entry.get("author", "").strip()

    if not title:
        return None

    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "abstract": abstract,
        "authors": authors,
        "url": link,
        "category": category,
    }


def fetch_new_papers() -> list[dict]:
    """
    config で指定された全カテゴリの arXiv RSS を取得し、
    論文情報の辞書リストを返す。
    """
    categories = CONFIG.get("arxiv", {}).get("categories", [])
    interval = CONFIG.get("arxiv", {}).get("request_interval_sec", 3.0)

    if not categories:
        logger.warning("arxiv.categories が未設定です")
        return []

    all_papers: list[dict] = []
    seen_ids: set[str] = set()

    for i, category in enumerate(categories):
        if i > 0:
            time.sleep(interval)

        url = _ARXIV_RSS_URL.format(category=category)
        logger.info(f"RSS 取得中: {url}")

        try:
            feed = feedparser.parse(url)
        except Exception as e:
            logger.error(f"RSS 取得エラー ({category}): {e}")
            continue

        if feed.bozo and not feed.entries:
            logger.warning(f"RSS パースエラー ({category}): {feed.bozo_exception}")
            continue

        count = 0
        for entry in feed.entries:
            paper = _parse_entry(entry, category)
            if paper and paper["arxiv_id"] not in seen_ids:
                seen_ids.add(paper["arxiv_id"])
                all_papers.append(paper)
                count += 1

        logger.info(f"{category}: {count} 件取得")

    logger.info(f"全カテゴリ合計: {len(all_papers)} 件")
    return all_papers
