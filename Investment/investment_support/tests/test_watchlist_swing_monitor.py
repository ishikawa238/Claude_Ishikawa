"""watchlist_swing_monitor / evaluate_watchlist_swing のテスト"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.swing_detector import evaluate_watchlist_swing


def _base_tk(**overrides):
    base = {
        "ticker": "TEST",
        "market": "US",
        "close": 25.0,
        "ema25": 30.0,
        "ema75": 28.0,
        "rsi14": 32.0,
        "atr14": 1.5,
        "volume": 1_000_000,
        "vol_20d_avg": 800_000,
        "macd_diff": 0.2,
        "macd_dea": 0.1,
        "macd_hist": 0.1,
        "macd_diff_prev": -0.1,
        "macd_dea_prev": 0.0,
        "macd_hist_prev": -0.1,
        "name": "Test Inc",
    }
    base.update(overrides)
    return base


class EvaluateWatchlistSwingTests(unittest.TestCase):
    def test_returns_row_when_all_strict(self):
        row = evaluate_watchlist_swing(_base_tk(), "2026-07-01", macd_require_both=True)
        self.assertIsNotNone(row)
        self.assertEqual(row["macd_confirm_type"], "both")

    def test_none_when_macd_hist_only(self):
        row = evaluate_watchlist_swing(
            _base_tk(
                macd_diff=-2.22,
                macd_dea=-1.33,
                macd_hist=-0.89,
                macd_diff_prev=-2.09,
                macd_dea_prev=-1.11,
                macd_hist_prev=-0.98,
            ),
            "2026-07-01",
            macd_require_both=True,
        )
        self.assertIsNone(row)

    def test_none_when_no_uptrend(self):
        row = evaluate_watchlist_swing(
            _base_tk(ema25=20.0, ema75=30.0),
            "2026-07-01",
            macd_require_both=True,
        )
        self.assertIsNone(row)


class FilterNewCandidatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""
            CREATE TABLE intraday_swing_alert_log (
                id INTEGER PRIMARY KEY,
                alert_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                market TEXT NOT NULL,
                label TEXT,
                notified INTEGER DEFAULT 0,
                first_alert_at TEXT,
                last_check_at TEXT,
                UNIQUE(alert_date, ticker, market)
            )
        """)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_filter_excludes_notified(self):
        from src.watchlist_swing_monitor import filter_new_candidates

        self.conn.execute(
            "INSERT INTO intraday_swing_alert_log "
            "(alert_date, ticker, market, label, notified) VALUES (?, ?, ?, ?, ?)",
            ("2026-07-01", "AAA", "US", "candidate", 1),
        )
        self.conn.commit()

        def _get_conn():
            return self.conn

        candidates = [
            {"ticker": "AAA", "market": "US", "date": "2026-07-01"},
            {"ticker": "BBB", "market": "US", "date": "2026-07-01"},
        ]
        with patch("src.watchlist_swing_monitor.get_connection") as mock_gc:
            mock_gc.return_value.__enter__ = lambda s: self.conn
            mock_gc.return_value.__exit__ = MagicMock(return_value=False)
            new = filter_new_candidates(candidates)
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["ticker"], "BBB")


if __name__ == "__main__":
    unittest.main()
