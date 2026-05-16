"""risk_control モジュールのテスト"""

import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch


_CONFIG_MOCK = {
    "capital": {
        "total_capital_jpy": "200000",
        "total_capital_usd": "0",
        "usdjpy_fallback": "150",
    },
    "risk_control": {
        "monthly_loss_stop_pct": "6",
        "max_consecutive_losses_before_pause": "3",
    },
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
}

_test_conn = None


def _setup_db():
    global _test_conn
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE trade_result_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actual_exit_date DATE,
            pnl_pct REAL,
            pnl_amount REAL
        )
    """)
    conn.commit()
    _test_conn = conn
    return conn


@contextmanager
def _fake_get_connection():
    yield _test_conn


def _fake_fetch_one(conn, sql, params=None):
    row = conn.execute(sql, params or ()).fetchone()
    return dict(row) if row else None


def _fake_fetch_all(conn, sql, params=None):
    return [dict(r) for r in conn.execute(sql, params or ()).fetchall()]


with patch("src.config.CONFIG", _CONFIG_MOCK), \
     patch("src.data_store.get_connection", _fake_get_connection), \
     patch("src.data_store.fetch_one", _fake_fetch_one), \
     patch("src.data_store.fetch_all", _fake_fetch_all):
    from src.risk_control import get_trade_pause_reason


class RiskControlTests(unittest.TestCase):

    def setUp(self):
        _setup_db()

    def test_monthly_loss_stop(self):
        _test_conn.execute(
            "INSERT INTO trade_result_log (actual_exit_date, pnl_pct, pnl_amount) VALUES (?, ?, ?)",
            ("2026-05-10", -7.0, -13000),
        )
        _test_conn.commit()

        reason = get_trade_pause_reason("2026-05-17")
        self.assertIn("月間損失", reason)

    def test_consecutive_losses_stop(self):
        rows = [
            ("2026-05-14", -1.0, -1000),
            ("2026-05-15", -2.0, -2000),
            ("2026-05-16", -3.0, -3000),
        ]
        _test_conn.executemany(
            "INSERT INTO trade_result_log (actual_exit_date, pnl_pct, pnl_amount) VALUES (?, ?, ?)",
            rows,
        )
        _test_conn.commit()

        reason = get_trade_pause_reason("2026-05-17")
        self.assertIn("連続損失", reason)

    def test_no_pause_when_under_limits(self):
        _test_conn.execute(
            "INSERT INTO trade_result_log (actual_exit_date, pnl_pct, pnl_amount) VALUES (?, ?, ?)",
            ("2026-05-16", 3.0, 3000),
        )
        _test_conn.commit()

        self.assertIsNone(get_trade_pause_reason("2026-05-17"))


if __name__ == "__main__":
    unittest.main()
