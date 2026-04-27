"""
ニュースフィルタモジュール

日付フィルタ（過去 N 時間以内）と DB ベースの URL 重複排除を行う。
"""

from datetime import datetime, timedelta, timezone

from src.config import CONFIG
from src.data_store import fetch_all, get_connection
from src.logger import get_logger

logger = get_logger(__name__)

_MAX_AGE_HOURS = int(CONFIG.get("filter", {}).get("max_age_hours", 24))
_MAX_PER_FEED = int(CONFIG.get("filter", {}).get("max_articles_per_feed", 10))


def filter_new_articles(articles: list[dict]) -> list[dict]:
    """
    記事リストに日付フィルタと重複排除を適用する。

    Args:
        articles: rss_fetcher から取得した全記事リスト

    Returns:
        フィルタ済みの新着記事リスト
    """
    # STEP 1: 日付フィルタ
    recent = _filter_by_date(articles)
    logger.info(f"日付フィルタ後: {len(recent)} 件（元: {len(articles)} 件）")

    # STEP 2: フィードごとの件数制限
    limited = _limit_per_feed(recent)
    logger.info(f"件数制限後: {len(limited)} 件")

    # STEP 3: DB 重複排除
    new_articles = _exclude_existing(limited)
    logger.info(f"重複排除後（新着）: {len(new_articles)} 件")

    return new_articles


def _filter_by_date(articles: list[dict]) -> list[dict]:
    """公開日が過去 N 時間以内の記事のみを残す。"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_MAX_AGE_HOURS)
    result = []
    for article in articles:
        pub = article.get("published_at")
        if pub is None:
            # 日付不明は残す（RSS に日付がないケースへの対応）
            result.append(article)
            continue
        try:
            dt = datetime.fromisoformat(pub)
            if dt >= cutoff:
                result.append(article)
        except (TypeError, ValueError):
            result.append(article)
    return result


def _limit_per_feed(articles: list[dict]) -> list[dict]:
    """フィードごとに最大件数を制限する。"""
    counts: dict[str, int] = {}
    result = []
    for article in articles:
        source = article.get("source", "")
        counts[source] = counts.get(source, 0) + 1
        if counts[source] <= _MAX_PER_FEED:
            result.append(article)
    return result


def _exclude_existing(articles: list[dict]) -> list[dict]:
    """DB に既に存在する URL の記事を除外する。"""
    if not articles:
        return []

    urls = [a["url"] for a in articles]
    placeholders = ",".join("?" * len(urls))
    sql = f"SELECT url FROM articles WHERE url IN ({placeholders})"

    with get_connection() as conn:
        rows = fetch_all(conn, sql, tuple(urls))

    existing_urls = {row["url"] for row in rows}
    return [a for a in articles if a["url"] not in existing_urls]
