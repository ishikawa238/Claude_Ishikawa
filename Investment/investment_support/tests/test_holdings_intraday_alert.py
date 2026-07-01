"""保有銘柄の毎時アラート判定・ダッシュボード更新テスト"""

import sqlite3
import tempfile
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from src.holdings_monitor import (
    classify_holdings_intraday_alert,
    filter_new_holding_alerts,
    upsert_intraday_price_history,
)


class ClassifyHoldingsAlertTests(unittest.TestCase):
    def test_stop_loss_label(self):
        h = {"label": "forced_stop", "current_price": 90, "stop_loss_price": 92}
        self.assertEqual(classify_holdings_intraday_alert(h), "stop_loss")

    def test_take_profit_label(self):
        h = {"label": "tp_full", "current_price": 120}
        self.assertEqual(classify_holdings_intraday_alert(h), "take_profit")

    def test_take_profit_by_price(self):
        h = {
            "label": "hold",
            "current_price": 125,
            "exit_effective_target_price": 120,
        }
        self.assertEqual(classify_holdings_intraday_alert(h), "take_profit")

    def test_no_alert(self):
        h = {"label": "hold", "current_price": 100, "exit_effective_target_price": 120}
        self.assertIsNone(classify_holdings_intraday_alert(h))


class FilterHoldingAlertsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(self.tmp.name + "/test.db")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""
            CREATE TABLE intraday_holding_alert_log (
                id INTEGER PRIMARY KEY,
                alert_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                market TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                notified INTEGER DEFAULT 0,
                first_alert_at TEXT,
                last_check_at TEXT,
                UNIQUE(alert_date, ticker, market, alert_type)
            )
        """)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_skips_already_notified(self):
        self.conn.execute(
            "INSERT INTO intraday_holding_alert_log "
            "(alert_date, ticker, market, alert_type, notified) "
            "VALUES ('2026-07-01', 'AAA', 'US', 'stop_loss', 1)",
        )
        self.conn.commit()
        holdings = [
            {"ticker": "AAA", "market": "US", "label": "forced_stop", "current_price": 10},
            {"ticker": "BBB", "market": "US", "label": "tp_full", "current_price": 20},
        ]
        with patch("src.holdings_monitor.get_connection") as mock_gc:
            mock_gc.return_value.__enter__ = lambda s: self.conn
            mock_gc.return_value.__exit__ = MagicMock(return_value=False)
            alerts = filter_new_holding_alerts(holdings, "2026-07-01")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["ticker"], "BBB")
        self.assertEqual(alerts[0]["alert_type"], "take_profit")


class UpsertIntradayPriceHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(self.tmp.name + "/test.db")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""
            CREATE TABLE price_history (
                id INTEGER PRIMARY KEY,
                ticker TEXT NOT NULL,
                date TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL,
                adj_close REAL, volume REAL, is_holiday INTEGER DEFAULT 0,
                UNIQUE(ticker, date)
            )
        """)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_upserts_today_bar_from_snapshot(self):
        snapshots = {
            "AAPL": {
                "last_price": 210.5,
                "open_price": 208.0,
                "high_price": 211.0,
                "low_price": 207.5,
                "volume": 12345,
            },
        }
        with patch("src.holdings_monitor.get_connection") as mock_gc:
            mock_gc.return_value.__enter__ = lambda s: self.conn
            mock_gc.return_value.__exit__ = MagicMock(return_value=False)
            count = upsert_intraday_price_history(snapshots, date(2026, 7, 1))
        self.assertEqual(count, 1)
        row = self.conn.execute(
            "SELECT adj_close, open, high, low, volume FROM price_history "
            "WHERE ticker='AAPL' AND date='2026-07-01'",
        ).fetchone()
        self.assertEqual(row["adj_close"], 210.5)
        self.assertEqual(row["open"], 208.0)
        self.assertEqual(row["volume"], 12345.0)

    def test_skips_empty_snapshots(self):
        with patch("src.holdings_monitor.get_connection") as mock_gc:
            mock_gc.return_value.__enter__ = lambda s: self.conn
            mock_gc.return_value.__exit__ = MagicMock(return_value=False)
            count = upsert_intraday_price_history({}, date(2026, 7, 1))
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
