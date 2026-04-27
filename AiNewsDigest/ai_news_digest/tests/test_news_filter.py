"""news_filter モジュールのテスト"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock


def _make_article(url="https://example.com/1", source="TestFeed", hours_ago=1):
    """テスト用の記事辞書を生成する。"""
    pub = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    return {
        "title": f"Test Article {url}",
        "url": url,
        "summary": "Test summary",
        "source": source,
        "published_at": pub,
    }


class TestFilterByDate(unittest.TestCase):
    """_filter_by_date のテスト"""

    def test_recent_articles_kept(self):
        from src.news_filter import _filter_by_date
        articles = [_make_article(hours_ago=1)]
        result = _filter_by_date(articles)
        self.assertEqual(len(result), 1)

    def test_old_articles_removed(self):
        from src.news_filter import _filter_by_date
        articles = [_make_article(hours_ago=48)]
        result = _filter_by_date(articles)
        self.assertEqual(len(result), 0)

    def test_no_date_kept(self):
        from src.news_filter import _filter_by_date
        article = _make_article()
        article["published_at"] = None
        result = _filter_by_date([article])
        self.assertEqual(len(result), 1)


class TestLimitPerFeed(unittest.TestCase):
    """_limit_per_feed のテスト"""

    def test_limit_applied(self):
        from src.news_filter import _limit_per_feed
        articles = [_make_article(url=f"https://example.com/{i}", source="A") for i in range(20)]
        result = _limit_per_feed(articles)
        self.assertLessEqual(len(result), 10)

    def test_different_sources_separate(self):
        from src.news_filter import _limit_per_feed
        articles = (
            [_make_article(url=f"https://a.com/{i}", source="A") for i in range(5)]
            + [_make_article(url=f"https://b.com/{i}", source="B") for i in range(5)]
        )
        result = _limit_per_feed(articles)
        self.assertEqual(len(result), 10)


class TestExcludeExisting(unittest.TestCase):
    """_exclude_existing のテスト"""

    @patch("src.news_filter.get_connection")
    @patch("src.news_filter.fetch_all")
    def test_existing_urls_excluded(self, mock_fetch_all, mock_conn):
        from src.news_filter import _exclude_existing

        mock_ctx = MagicMock()
        mock_conn.return_value.__enter__ = lambda s: mock_ctx
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)
        mock_fetch_all.return_value = [{"url": "https://example.com/1"}]

        articles = [
            _make_article(url="https://example.com/1"),
            _make_article(url="https://example.com/2"),
        ]
        result = _exclude_existing(articles)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["url"], "https://example.com/2")

    @patch("src.news_filter.get_connection")
    @patch("src.news_filter.fetch_all")
    def test_empty_list_returns_empty(self, mock_fetch_all, mock_conn):
        from src.news_filter import _exclude_existing
        result = _exclude_existing([])
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
