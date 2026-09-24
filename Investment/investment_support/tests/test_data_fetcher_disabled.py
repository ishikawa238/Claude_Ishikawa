"""eodhd.enabled=false 時に EODHD へ HTTP を送らないことのテスト。"""

import unittest
from datetime import date
from unittest.mock import patch

_CONFIG_MOCK = {
    "eodhd": {"api_key": "test", "base_url": "https://example.com", "request_interval_sec": 0,
              "enabled": False},
    "screening": {},
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
    "logging": {"log_dir": "/tmp", "log_level": "INFO"},
}

with patch("src.config.CONFIG", _CONFIG_MOCK):
    from src import data_fetcher


class EodhdDisabledTests(unittest.TestCase):

    @patch.object(data_fetcher, "_ENABLED", False)
    @patch("src.data_fetcher.requests.get")
    def test_disabled_returns_none_without_http(self, mock_get):
        self.assertIsNone(data_fetcher._get("https://example.com/eod/X", {}))
        mock_get.assert_not_called()

    @patch.object(data_fetcher, "_ENABLED", False)
    @patch("src.data_fetcher.requests.get")
    def test_index_history_is_empty_when_disabled(self, mock_get):
        """呼び出し元は空リストを受け取り、yfinance フォールバックに進める"""
        rows = data_fetcher.fetch_index_history("N225", date(2026, 9, 20), date(2026, 9, 24))
        self.assertEqual(rows, [])
        mock_get.assert_not_called()

    @patch.object(data_fetcher, "_ENABLED", False)
    @patch("src.data_fetcher.requests.get")
    def test_bulk_eod_is_empty_when_disabled(self, mock_get):
        self.assertEqual(data_fetcher.fetch_bulk_eod("US"), [])
        mock_get.assert_not_called()

    @patch.object(data_fetcher, "_ENABLED", True)
    @patch("src.data_fetcher.time.sleep")
    @patch("src.data_fetcher.requests.get")
    def test_enabled_sends_http(self, mock_get, _sleep):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = [{"date": "2026-09-24"}]
        self.assertEqual(data_fetcher._get("https://example.com/eod/X", {}), [{"date": "2026-09-24"}])
        mock_get.assert_called_once()


if __name__ == "__main__":
    unittest.main()
