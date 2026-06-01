"""
benchmark_comparison モジュールのテスト
"""

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

_CONFIG_MOCK = {
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
    "logging": {"log_dir": "/tmp", "log_level": "INFO"},
    "investment_goal": {"monthly_deposit_jpy": 10000, "target_monthly_return_pct": 15.0},
}

import src.config as _src_config

_src_config.CONFIG = _CONFIG_MOCK


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE portfolio_snapshot (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date DATE NOT NULL,
            batch_type TEXT NOT NULL,
            capital_jpy REAL NOT NULL DEFAULT 0,
            capital_usd REAL NOT NULL DEFAULT 0,
            holdings_value_jpy REAL NOT NULL DEFAULT 0,
            holdings_value_usd REAL NOT NULL DEFAULT 0,
            unrealized_pnl_jpy REAL NOT NULL DEFAULT 0,
            unrealized_pnl_usd REAL NOT NULL DEFAULT 0,
            total_assets_jpy REAL NOT NULL DEFAULT 0,
            total_assets_usd REAL NOT NULL DEFAULT 0,
            total_assets_combined_jpy REAL NOT NULL,
            cash_jpy REAL NOT NULL DEFAULT 0,
            cash_usd REAL NOT NULL DEFAULT 0,
            paypay_points_value_jpy REAL NOT NULL DEFAULT 0,
            usdjpy_rate REAL NOT NULL DEFAULT 150,
            holdings_count INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE index_snapshot (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            index_code TEXT NOT NULL,
            date DATE NOT NULL,
            close REAL NOT NULL,
            ma25 REAL,
            ma75 REAL,
            ma200 REAL,
            UNIQUE(index_code, date)
        );
    """)
    return conn


class BenchmarkComparisonTests(unittest.TestCase):
    def test_build_comparison_normalized_and_excess(self):
        from contextlib import contextmanager
        from src import benchmark_comparison as bc

        conn = _make_db()
        today = date.today()
        d1 = today.fromordinal(today.toordinal() - 30).isoformat()
        d2 = today.fromordinal(today.toordinal() - 15).isoformat()
        # 資産: 100万 → 110万（+10%）、同月内なので入金控除も +10%
        conn.execute(
            "INSERT INTO portfolio_snapshot "
            "(snapshot_date, batch_type, total_assets_combined_jpy, capital_jpy, capital_usd, usdjpy_rate) "
            "VALUES (?, 'morning', 1000000, 0, 0, 150)",
            (d1,),
        )
        conn.execute(
            "INSERT INTO portfolio_snapshot "
            "(snapshot_date, batch_type, total_assets_combined_jpy, capital_jpy, capital_usd, usdjpy_rate) "
            "VALUES (?, 'evening', 1100000, 0, 0, 150)",
            (d2,),
        )
        # SP500: 5000 → 5250（+5%）
        conn.execute(
            "INSERT INTO index_snapshot (index_code, date, close) VALUES ('SP500', ?, 5000)",
            (d1,),
        )
        conn.execute(
            "INSERT INTO index_snapshot (index_code, date, close) VALUES ('SP500', ?, 5250)",
            (d2,),
        )
        conn.commit()

        @contextmanager
        def _cm():
            yield conn

        with patch("src.benchmark_comparison.get_connection", _cm):
            result = bc.build_benchmark_comparison(days=365)

        self.assertEqual(len(result["dates"]), 2)
        self.assertEqual(result["portfolio_index"], [100.0, 110.0])
        self.assertEqual(result["sp500_index"], [100.0, 105.0])
        s = result["summary"]
        self.assertEqual(s["portfolio_return_pct"], 10.0)
        self.assertEqual(s["sp500_return_pct"], 5.0)
        self.assertEqual(s["excess_adjusted_return_pct"], 5.0)
        self.assertTrue(s["beating_sp500"])

    def test_insufficient_data_returns_message(self):
        from contextlib import contextmanager
        from src import benchmark_comparison as bc

        conn = _make_db()

        @contextmanager
        def _cm():
            yield conn

        with patch("src.benchmark_comparison.get_connection", _cm):
            result = bc.build_benchmark_comparison(days=90)

        self.assertEqual(result["dates"], [])
        self.assertIsNotNone(result["meta"]["message"])


if __name__ == "__main__":
    unittest.main()
