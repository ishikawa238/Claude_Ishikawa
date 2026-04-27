"""notifier モジュールのテスト"""

import unittest
from datetime import date
from unittest.mock import patch


class TestArticleBlock(unittest.TestCase):
    """_article_block のテスト"""

    def test_basic_block(self):
        from src.notifier import _article_block
        article = {
            "title": "GPT-5 Released",
            "url": "https://example.com/gpt5",
            "summary": "OpenAI releases GPT-5",
        }
        block = _article_block(article)
        self.assertEqual(block["type"], "section")
        text = block["text"]["text"]
        self.assertIn("GPT-5 Released", text)
        self.assertIn("https://example.com/gpt5", text)

    def test_no_summary(self):
        from src.notifier import _article_block
        article = {"title": "Test", "url": "https://example.com", "summary": ""}
        block = _article_block(article)
        text = block["text"]["text"]
        self.assertIn("Test", text)

    def test_long_summary_truncated(self):
        from src.notifier import _article_block
        article = {
            "title": "Test",
            "url": "https://example.com",
            "summary": "X" * 300,
        }
        block = _article_block(article)
        text = block["text"]["text"]
        self.assertIn("…", text)


class TestSendNewsDigest(unittest.TestCase):
    """send_news_digest のテスト"""

    @patch("src.notifier.send_webhook", return_value=True)
    def test_sends_when_articles_exist(self, mock_send):
        from src.notifier import send_news_digest
        articles = [
            {"title": "Test", "url": "https://example.com/1", "summary": "Summary", "source": "Feed A"},
        ]
        result = send_news_digest(date(2026, 4, 26), articles, {"total_fetched": 10})
        self.assertTrue(result)
        mock_send.assert_called_once()

        payload = mock_send.call_args[0][1]
        self.assertIn("blocks", payload)

    @patch("src.notifier.send_webhook")
    def test_skips_when_no_articles(self, mock_send):
        from src.notifier import send_news_digest
        result = send_news_digest(date(2026, 4, 26), [], {"total_fetched": 0})
        self.assertTrue(result)
        mock_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
