"""スクリーニング健全性（流動性フィルタ・診断）のテスト"""

import sys
import sqlite3
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

import src.config as _src_config
from src.market_filter import diagnose_screening_empty, get_screened_tickers
from src.shares_outstanding import load_shares_map, refresh_us_shares_outstanding

_CONFIG_MOCK = {
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
    "logging": {"log_dir": "/tmp", "log_level": "INFO"},
    "screening": {
        "jp_min_price": 300,
        "jp_min_turnover": 300000000,
        "jp_min_market_cap": 30000000000,
        "us_min_price": 5,
        "us_min_turnover": 20000000,
        "us_min_market_cap": 2000000000,
        "min_active_days_of_30": 20,
        "allow_unknown_market_cap": True,
        "us_shares_refresh_max_per_monthly": 5,
        "us_shares_refresh_interval_sec": 0,
    },
    "jquants": {"api_key": "", "request_interval_sec": 1.0},
    "eodhd": {"api_key": "test", "base_url": "https://eodhd.com/api"},
}

_test_conn = None
_SAMPLE_ROW = {"ticker": "7203", "date": "2026-05-29", "market": "JP", "name": "Toyota"}


def _get_test_connection():
    return _test_conn


class ScreeningHealthTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _test_conn
        _src_config.CONFIG = _CONFIG_MOCK
        import src.market_filter as mf
        import src.shares_outstanding as so

        mf.CONFIG = _CONFIG_MOCK
        mf._SC = _CONFIG_MOCK["screening"]
        mf._ALLOW_UNKNOWN_MARKET_CAP = True
        so.CONFIG = _CONFIG_MOCK
        so._SC = _CONFIG_MOCK["screening"]

        _test_conn = sqlite3.connect(":memory:")
        _test_conn.row_factory = sqlite3.Row
        _test_conn.executescript("""
            CREATE TABLE universe_master (
                ticker TEXT PRIMARY KEY, name TEXT, market TEXT,
                is_active INTEGER DEFAULT 1, is_excluded INTEGER DEFAULT 0,
                shares_outstanding REAL, last_updated TEXT
            );
            CREATE TABLE technical_snapshot (ticker TEXT, date TEXT, ma200 REAL);
        """)
        _test_conn.execute(
            "INSERT INTO universe_master (ticker, name, market) VALUES ('7203', 'Toyota', 'JP')"
        )
        _test_conn.execute(
            "INSERT INTO technical_snapshot (ticker, date, ma200) VALUES ('7203', ?, 2800)",
            (date.today().strftime("%Y-%m-%d"),),
        )
        _test_conn.commit()

        cls._patches = [
            patch("src.data_store.get_connection", _get_test_connection),
            patch("src.market_filter.get_connection", _get_test_connection),
            patch("src.shares_outstanding.get_connection", _get_test_connection),
        ]
        for p in cls._patches:
            p.start()

    @classmethod
    def tearDownClass(cls):
        for p in cls._patches:
            p.stop()
        _test_conn.close()

    @patch("src.market_filter._filter_inactive", side_effect=lambda rows, _d: rows)
    @patch("src.market_filter._fetch_liquidity_candidates", return_value=[_SAMPLE_ROW])
    def test_get_screened_delegates_to_liquidity_pipeline(self, _fetch, _inactive):
        today = date.today()
        rows = get_screened_tickers(today, "JP")
        self.assertEqual(len(rows), 1)
        _fetch.assert_called_once()
        _inactive.assert_called_once()

    @patch("src.market_filter._fetch_liquidity_candidates", return_value=[])
    @patch("src.shares_outstanding.count_shares_known", return_value=0)
    def test_diagnose_when_market_cap_required_but_missing(self, _shares, _fetch):
        import src.market_filter as mf

        mf._ALLOW_UNKNOWN_MARKET_CAP = False
        today = date.today()
        msg = diagnose_screening_empty(today, "JP")
        self.assertIsNotNone(msg)
        self.assertIn("時価総額", msg)
        mf._ALLOW_UNKNOWN_MARKET_CAP = True

    def test_load_shares_map(self):
        _test_conn.execute(
            "UPDATE universe_master SET shares_outstanding = 1000000 WHERE ticker = '7203'"
        )
        _test_conn.commit()
        m = load_shares_map(["7203", "MISSING"])
        self.assertEqual(m["7203"], 1000000.0)
        self.assertNotIn("MISSING", m)

    @patch("src.data_fetcher.fetch_shares_outstanding")
    def test_refresh_us_shares(self, mock_fetch):
        mock_fetch.return_value = 5000000.0
        _test_conn.execute(
            "INSERT INTO universe_master (ticker, name, market) "
            "VALUES ('AAPL', 'Apple', 'US')"
        )
        _test_conn.commit()
        n = refresh_us_shares_outstanding(date.today(), max_updates=10)
        self.assertEqual(n, 1)
        row = _test_conn.execute(
            "SELECT shares_outstanding FROM universe_master WHERE ticker = 'AAPL'"
        ).fetchone()
        self.assertEqual(row[0], 5000000.0)


if __name__ == "__main__":
    unittest.main()
