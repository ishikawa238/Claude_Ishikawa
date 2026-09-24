"""moomoo OpenD 未起動時に SDK を呼ばずスキップすることのテスト。

SDK は OpenD 未起動だと接続を無期限に再試行して戻らず、朝バッチがタイムアウトしていた。
"""

import socket
import unittest
from datetime import date
from unittest.mock import patch

_CONFIG_MOCK = {
    "moomoo": {"host": "127.0.0.1", "port": 11111},
    "moomoo_watchlist": {"enabled": True},
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
    "logging": {"log_dir": "/tmp", "log_level": "INFO"},
}

with patch("src.config.CONFIG", _CONFIG_MOCK):
    from shared.net import is_port_open
    from src import moomoo_quote, moomoo_sync, moomoo_watchlist


class IsPortOpenTests(unittest.TestCase):

    def test_listening_port_is_open(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            self.assertTrue(is_port_open("127.0.0.1", port, timeout=1.0))

    def test_closed_port_is_not_open(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.assertFalse(is_port_open("127.0.0.1", port, timeout=1.0))


@patch("src.moomoo_sync.CONFIG", _CONFIG_MOCK)
@patch("src.moomoo_sync.is_port_open", return_value=False)
class MoomooSyncGuardTests(unittest.TestCase):

    @patch("src.moomoo_sync._fetch_positions")
    def test_sync_from_moomoo_skips(self, mock_fetch, _port):
        self.assertEqual(moomoo_sync.sync_from_moomoo(), (0, 0))
        mock_fetch.assert_not_called()

    @patch("src.moomoo_sync._fetch_deal_history")
    def test_sync_trade_results_skips(self, mock_fetch, _port):
        self.assertEqual(moomoo_sync.sync_trade_results(), 0)
        mock_fetch.assert_not_called()

    def test_fetch_account_cash_returns_zero(self, _port):
        self.assertEqual(moomoo_sync.fetch_account_cash(), {"cash_jpy": 0.0, "cash_usd": 0.0})


class MoomooWatchlistGuardTests(unittest.TestCase):

    @patch("src.moomoo_watchlist.is_port_open", return_value=False)
    @patch("src.moomoo_watchlist.fetch_candidate_tickers")
    @patch("src.moomoo_watchlist._get_watchlist_cfg", return_value={"group_name": "システム候補"})
    def test_sync_watchlist_skips(self, _cfg, mock_candidates, _port):
        result = moomoo_watchlist.sync_watchlist("US", date(2026, 9, 24))
        self.assertTrue(result["skipped"])
        self.assertEqual(result["error"], "opend_unreachable")
        mock_candidates.assert_not_called()

    @patch("src.moomoo_watchlist.is_port_open", return_value=False)
    @patch("src.moomoo_watchlist._resolve_group_name")
    @patch("src.moomoo_watchlist._get_watchlist_monitor_cfg", return_value={"markets": ["US"]})
    def test_fetch_watchlist_tickers_returns_empty(self, _cfg, mock_resolve, _port):
        self.assertEqual(moomoo_watchlist.fetch_watchlist_tickers(), [])
        mock_resolve.assert_not_called()


class MoomooQuoteGuardTests(unittest.TestCase):

    @patch("src.moomoo_quote.is_port_open", return_value=False)
    def test_client_raises_connection_error(self, _port):
        with self.assertRaises(ConnectionError):
            moomoo_quote.MoomooQuoteClient("127.0.0.1", 11111)


if __name__ == "__main__":
    unittest.main()
