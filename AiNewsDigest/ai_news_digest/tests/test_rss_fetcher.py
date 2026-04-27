"""rss_fetcher モジュールのテスト"""

import unittest
from unittest.mock import patch, MagicMock
from types import SimpleNamespace


class TestExtractSummary(unittest.TestCase):
    """_extract_summary のテスト"""

    def test_plain_text(self):
        from src.rss_fetcher import _extract_summary
        entry = {"summary": "AIの最新動向について解説します。"}
        self.assertEqual(_extract_summary(entry), "AIの最新動向について解説します。")

    def test_html_tags_removed(self):
        from src.rss_fetcher import _extract_summary
        entry = {"summary": "<p>AIの<b>最新</b>動向</p>"}
        self.assertEqual(_extract_summary(entry), "AIの最新動向")

    def test_truncated_at_500(self):
        from src.rss_fetcher import _extract_summary
        entry = {"summary": "A" * 600}
        result = _extract_summary(entry)
        self.assertEqual(len(result), 500)

    def test_empty_summary(self):
        from src.rss_fetcher import _extract_summary
        entry = {"summary": ""}
        self.assertEqual(_extract_summary(entry), "")

    def test_fallback_to_description(self):
        from src.rss_fetcher import _extract_summary
        entry = {"summary": "", "description": "フォールバックの説明"}
        self.assertEqual(_extract_summary(entry), "フォールバックの説明")


class TestParsePublished(unittest.TestCase):
    """_parse_published のテスト"""

    def test_valid_published_parsed(self):
        from src.rss_fetcher import _parse_published
        entry = {"published_parsed": (2026, 4, 25, 10, 30, 0, 4, 115, 0)}
        result = _parse_published(entry)
        self.assertIsNotNone(result)
        self.assertIn("2026-04-25", result)

    def test_none_when_no_date(self):
        from src.rss_fetcher import _parse_published
        entry = {}
        self.assertIsNone(_parse_published(entry))

    def test_fallback_to_updated_parsed(self):
        from src.rss_fetcher import _parse_published
        entry = {"updated_parsed": (2026, 4, 24, 8, 0, 0, 3, 114, 0)}
        result = _parse_published(entry)
        self.assertIsNotNone(result)
        self.assertIn("2026-04-24", result)


if __name__ == "__main__":
    unittest.main()
