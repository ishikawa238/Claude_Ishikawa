"""rss_fetcher モジュールのユニットテスト"""

import unittest
from unittest.mock import MagicMock, patch

MOCK_CONFIG = {
    "arxiv": {
        "categories": ["cs.CR"],
        "request_interval_sec": 0,
    },
    "logging": {"log_dir": "./logs", "log_level": "WARNING"},
}


class TestParseEntry(unittest.TestCase):
    """_parse_entry のテスト"""

    def setUp(self):
        self._patcher = patch("src.rss_fetcher.CONFIG", MOCK_CONFIG)
        self._patcher.start()
        from src.rss_fetcher import _parse_entry
        self._parse_entry = _parse_entry

    def tearDown(self):
        self._patcher.stop()

    def test_valid_entry(self):
        """正常なエントリからフィールドが正しく抽出される"""
        entry = {
            "link": "http://arxiv.org/abs/2401.12345",
            "title": "AML on Blockchain",
            "summary": "We propose a method...",
            "author": "Alice, Bob",
        }
        result = self._parse_entry(entry, "cs.CR")

        self.assertIsNotNone(result)
        self.assertEqual(result["arxiv_id"], "2401.12345")
        self.assertEqual(result["title"], "AML on Blockchain")
        self.assertEqual(result["abstract"], "We propose a method...")
        self.assertEqual(result["authors"], "Alice, Bob")
        self.assertEqual(result["url"], "http://arxiv.org/abs/2401.12345")
        self.assertEqual(result["category"], "cs.CR")

    def test_five_digit_arxiv_id(self):
        """5桁の arxiv_id も正しく抽出される"""
        entry = {
            "link": "http://arxiv.org/abs/2401.12345",
            "title": "Test",
            "summary": "",
            "author": "",
        }
        result = self._parse_entry(entry, "cs.CR")
        self.assertEqual(result["arxiv_id"], "2401.12345")

    def test_versioned_url(self):
        """バージョン付き URL からも arxiv_id が抽出される"""
        entry = {
            "link": "http://arxiv.org/abs/2401.12345v2",
            "title": "Test",
            "summary": "",
            "author": "",
        }
        result = self._parse_entry(entry, "cs.CR")
        self.assertEqual(result["arxiv_id"], "2401.12345")

    def test_invalid_link_returns_none(self):
        """arxiv_id が抽出できない場合は None を返す"""
        entry = {"link": "https://example.com", "title": "Test"}
        result = self._parse_entry(entry, "cs.CR")
        self.assertIsNone(result)

    def test_empty_title_returns_none(self):
        """タイトルが空の場合は None を返す"""
        entry = {
            "link": "http://arxiv.org/abs/2401.12345",
            "title": "",
            "summary": "Abstract",
            "author": "",
        }
        result = self._parse_entry(entry, "cs.CR")
        self.assertIsNone(result)

    def test_required_keys_present(self):
        """返り値に必要なキーが全て含まれる"""
        entry = {
            "link": "http://arxiv.org/abs/2401.00001",
            "title": "Test Paper",
            "summary": "Abstract text",
            "author": "Author",
        }
        result = self._parse_entry(entry, "q-fin.RM")
        required = {"arxiv_id", "title", "abstract", "authors", "url", "category"}
        self.assertEqual(required, set(result.keys()))


class TestFetchNewPapers(unittest.TestCase):
    """fetch_new_papers の結合テスト（feedparser をモック）"""

    @patch("src.rss_fetcher.feedparser")
    def test_fetches_and_deduplicates(self, mock_fp):
        """複数カテゴリで重複する論文が除外される"""
        with patch("src.rss_fetcher.CONFIG", {
            **MOCK_CONFIG,
            "arxiv": {"categories": ["cs.CR", "q-fin.RM"], "request_interval_sec": 0},
        }):
            from src.rss_fetcher import fetch_new_papers

            entry = MagicMock()
            entry.__getitem__ = lambda self, k: {
                "link": "http://arxiv.org/abs/2401.11111",
                "title": "Paper A",
                "summary": "Abstract",
                "author": "Author",
            }.get(k, "")
            entry.get = lambda k, d="": {
                "link": "http://arxiv.org/abs/2401.11111",
                "title": "Paper A",
                "summary": "Abstract",
                "author": "Author",
            }.get(k, d)

            mock_feed = MagicMock()
            mock_feed.bozo = False
            mock_feed.entries = [entry]
            mock_fp.parse.return_value = mock_feed

            papers = fetch_new_papers()

            # 同じ arxiv_id が2カテゴリに出ても1件のみ
            self.assertEqual(len(papers), 1)
            self.assertEqual(papers[0]["arxiv_id"], "2401.11111")


if __name__ == "__main__":
    unittest.main()
