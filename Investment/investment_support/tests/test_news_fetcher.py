"""
news_fetcher.py のテスト

Google News RSS の取得・パース・フォーマットを検証する。
"""

import unittest
from unittest.mock import patch, MagicMock

# DB 依存なしで直接 import
with patch("src.config.CONFIG", {
    "slack": {"webhook_url": "https://XXXXX"},
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
    "signal": {},
    "capital": {"total_capital_jpy": "1000000", "total_capital_usd": "10000",
                "max_risk_per_trade_pct": "2", "default_stop_loss_pct": "8"},
}):
    from src import news_fetcher


_SAMPLE_RSS = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test News</title>
    <item>
      <title>日経平均、38000円台回復 - 日本経済新聞</title>
      <pubDate>Thu, 08 May 2026 10:00:00 GMT</pubDate>
      <source url="https://example.com">日本経済新聞</source>
    </item>
    <item>
      <title>トヨタ、EV新型車発表 - 朝日新聞</title>
      <pubDate>Wed, 07 May 2026 08:00:00 GMT</pubDate>
      <source url="https://example.com">朝日新聞</source>
    </item>
    <item>
      <title>米金利据え置きを発表 - ロイター</title>
      <pubDate>Wed, 07 May 2026 22:00:00 GMT</pubDate>
      <source url="https://example.com">ロイター</source>
    </item>
  </channel>
</rss>
"""


class TestParseRss(unittest.TestCase):
    """_parse_rss のテスト"""

    def test_parse_valid_rss(self):
        """有効なRSSをパースして正しい件数のアイテムが返る"""
        items = news_fetcher._parse_rss(_SAMPLE_RSS)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["title"], "日経平均、38000円台回復")
        self.assertEqual(items[0]["published"], "2026-05-08")
        self.assertEqual(items[0]["source"], "日本経済新聞")

    def test_parse_max_items(self):
        """max_items で件数が制限される"""
        items = news_fetcher._parse_rss(_SAMPLE_RSS, max_items=1)
        self.assertEqual(len(items), 1)

    def test_parse_invalid_xml(self):
        """無効なXMLで空リストが返る"""
        items = news_fetcher._parse_rss("<invalid>not xml")
        self.assertEqual(items, [])

    def test_parse_empty_rss(self):
        """空のRSSで空リストが返る"""
        items = news_fetcher._parse_rss(
            '<?xml version="1.0"?><rss><channel></channel></rss>'
        )
        self.assertEqual(items, [])

    def test_source_extraction_from_title(self):
        """<source> タグがない場合、タイトルから ' - ソース' を分離する"""
        rss = """\
<?xml version="1.0"?>
<rss><channel>
  <item>
    <title>重要ニュース - テストメディア</title>
    <pubDate>Thu, 08 May 2026 10:00:00 GMT</pubDate>
  </item>
</channel></rss>"""
        items = news_fetcher._parse_rss(rss)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "重要ニュース")
        self.assertEqual(items[0]["source"], "テストメディア")


class TestParsePubDate(unittest.TestCase):
    """_parse_pub_date のテスト"""

    def test_valid_date(self):
        self.assertEqual(
            news_fetcher._parse_pub_date("Thu, 08 May 2026 10:00:00 GMT"),
            "2026-05-08",
        )

    def test_empty_date(self):
        self.assertEqual(news_fetcher._parse_pub_date(None), "")
        self.assertEqual(news_fetcher._parse_pub_date(""), "")

    def test_invalid_date(self):
        self.assertEqual(news_fetcher._parse_pub_date("not a date"), "")


class TestCleanCompanyName(unittest.TestCase):
    """_clean_company_name のテスト"""

    def test_remove_kabushiki_kaisha(self):
        self.assertEqual(news_fetcher._clean_company_name("株式会社トヨタ自動車"), "トヨタ自動車")

    def test_remove_inc(self):
        self.assertEqual(news_fetcher._clean_company_name("Apple Inc."), "Apple")

    def test_no_change(self):
        self.assertEqual(news_fetcher._clean_company_name("ソニーグループ"), "ソニーグループ")


class TestFetchMarketNews(unittest.TestCase):
    """fetch_market_news のモックテスト"""

    @patch("src.news_fetcher._fetch_google_news_rss")
    def test_jp_market_news(self, mock_rss):
        """JP市場ニュースを取得して重複除去・件数制限する"""
        mock_rss.return_value = [
            {"title": "ニュース1", "published": "2026-05-08", "source": "A"},
            {"title": "ニュース2", "published": "2026-05-07", "source": "B"},
            {"title": "ニュース1", "published": "2026-05-08", "source": "A"},  # 重複
        ]
        result = news_fetcher.fetch_market_news(market="JP", limit=5)
        self.assertEqual(len(result), 2)  # 重複除去後
        self.assertEqual(result[0]["title"], "ニュース1")  # 日付降順

    @patch("src.news_fetcher._fetch_google_news_rss")
    def test_limit_respected(self, mock_rss):
        """limit で件数が制限される"""
        mock_rss.return_value = [
            {"title": f"ニュース{i}", "published": "2026-05-08", "source": "A"}
            for i in range(10)
        ]
        result = news_fetcher.fetch_market_news(market="JP", limit=3)
        self.assertEqual(len(result), 3)

    @patch("src.news_fetcher._fetch_google_news_rss")
    def test_rss_failure_returns_empty(self, mock_rss):
        """RSS取得失敗時は空リスト"""
        mock_rss.return_value = []
        result = news_fetcher.fetch_market_news(market="JP")
        self.assertEqual(result, [])


class TestFetchTickerNews(unittest.TestCase):
    """fetch_ticker_news のモックテスト"""

    @patch("src.news_fetcher._fetch_google_news_rss")
    def test_ticker_news_with_name(self, mock_rss):
        """企業名でニュースを検索し ticker が付与される"""
        mock_rss.return_value = [
            {"title": "トヨタ増収", "published": "2026-05-08", "source": "日経"},
        ]
        result = news_fetcher.fetch_ticker_news(
            [("7203", "トヨタ自動車")], limit_per_ticker=2
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["ticker"], "7203")
        self.assertEqual(result[0]["title"], "トヨタ増収")

    @patch("src.news_fetcher._fetch_google_news_rss")
    def test_skip_ticker_without_name(self, mock_rss):
        """企業名がない場合はスキップ"""
        result = news_fetcher.fetch_ticker_news(
            [("7203", ""), ("AAPL", "AAPL")], limit_per_ticker=2
        )
        self.assertEqual(result, [])
        mock_rss.assert_not_called()


class TestFetchGoogleNewsRss(unittest.TestCase):
    """_fetch_google_news_rss のモックテスト"""

    @patch("src.news_fetcher.requests.get")
    def test_successful_fetch(self, mock_get):
        """正常取得でパース結果が返る"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = _SAMPLE_RSS
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        items = news_fetcher._fetch_google_news_rss("日経平均", market="JP")
        self.assertEqual(len(items), 3)
        mock_get.assert_called_once()

    @patch("src.news_fetcher.requests.get")
    def test_http_error_returns_empty(self, mock_get):
        """HTTPエラーで空リスト"""
        import requests as req
        mock_get.side_effect = req.exceptions.ConnectionError("connection failed")
        items = news_fetcher._fetch_google_news_rss("日経平均")
        self.assertEqual(items, [])


if __name__ == "__main__":
    unittest.main()
