"""trading_calendar / スクリーニング日付不一致診断のテスト"""

import sqlite3
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

import src.config as _src_config
from src.market_filter import diagnose_screening_empty
from src.trading_calendar import latest_price_date_for_market, resolve_snapshot_date

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
    },
}

_test_conn = None


def _get_test_connection():
    return _test_conn


class TradingCalendarTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _test_conn
        _src_config.CONFIG = _CONFIG_MOCK
        import src.market_filter as mf

        mf.CONFIG = _CONFIG_MOCK
        mf._SC = _CONFIG_MOCK["screening"]

        _test_conn = sqlite3.connect(":memory:")
        _test_conn.row_factory = sqlite3.Row
        _test_conn.executescript("""
            CREATE TABLE universe_master (
                ticker TEXT PRIMARY KEY, market TEXT,
                is_active INTEGER DEFAULT 1, is_excluded INTEGER DEFAULT 0
            );
            CREATE TABLE price_history (
                ticker TEXT, date TEXT, adj_close REAL, volume REAL,
                is_holiday INTEGER DEFAULT 0
            );
            CREATE TABLE technical_snapshot (ticker TEXT, date TEXT, ma200 REAL);
        """)
        _test_conn.execute(
            "INSERT INTO universe_master (ticker, market) VALUES ('7203', 'JP')"
        )
        _test_conn.execute(
            "INSERT INTO price_history (ticker, date, adj_close, volume, is_holiday) "
            "VALUES ('7203', '2026-05-29', 3000, 1000000, 0)"
        )
        _test_conn.execute(
            "INSERT INTO technical_snapshot (ticker, date, ma200) VALUES ('7203', '2026-05-30', 2800)"
        )
        _test_conn.commit()

        cls._patches = [
            patch("src.data_store.get_connection", _get_test_connection),
            patch("src.trading_calendar.get_connection", _get_test_connection),
            patch("src.market_filter.get_connection", _get_test_connection),
        ]
        for p in cls._patches:
            p.start()

    @classmethod
    def tearDownClass(cls):
        for p in cls._patches:
            p.stop()
        _test_conn.close()

    def test_latest_price_date_for_market(self):
        d = latest_price_date_for_market(date(2026, 5, 30), "JP")
        self.assertEqual(d, date(2026, 5, 29))

    def test_resolve_snapshot_date_from_rows(self):
        rows = [{"date": "2026-05-29", "ticker": "7203"}]
        d = resolve_snapshot_date(date(2026, 5, 30), "JP", rows)
        self.assertEqual(d, date(2026, 5, 29))

    def test_resolve_snapshot_date_prefers_latest_covered_date(self):
        """取引の薄い銘柄で直近日の行が欠けても、半分以上揃っていれば最新日を使う"""
        rows = (
            [{"date": "2026-09-21", "ticker": f"T{i}"} for i in range(100)]
            + [{"date": "2026-09-22", "ticker": f"T{i}"} for i in range(80)]
            + [{"date": "2026-09-23", "ticker": f"T{i}"} for i in range(60)]
        )
        d = resolve_snapshot_date(date(2026, 9, 24), "US", rows)
        self.assertEqual(d, date(2026, 9, 23))

    def test_resolve_snapshot_date_ignores_sparse_latest_date(self):
        """件数が最多日の半分未満の日付（紛れ込んだ1件など）は採用しない"""
        rows = (
            [{"date": "2026-09-23", "ticker": f"T{i}"} for i in range(100)]
            + [{"date": "2026-09-24", "ticker": "T0"}]
        )
        d = resolve_snapshot_date(date(2026, 9, 24), "US", rows)
        self.assertEqual(d, date(2026, 9, 23))

    @patch("src.market_filter._fetch_liquidity_candidates", return_value=[])
    def test_diagnose_date_mismatch(self, _fetch):
        msg = diagnose_screening_empty(date(2026, 5, 30), "JP")
        self.assertIsNotNone(msg)
        self.assertIn("日付不一致", msg)
        self.assertIn("2026-05-29", msg)


if __name__ == "__main__":
    unittest.main()
