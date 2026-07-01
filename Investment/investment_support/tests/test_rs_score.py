"""rs_score モジュールのテスト"""

import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from src.rs_score import (
    apply_rs_filter,
    dynamic_rsi_threshold,
    index_return_pct,
    percentile_scores,
    update_rs_scores,
)


class PercentileScoresTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(percentile_scores([]), {})

    def test_single(self):
        self.assertEqual(percentile_scores([("A", 1.0)]), {"A": 50.0})

    def test_three_tickers(self):
        scores = percentile_scores([("A", -5.0), ("B", 0.0), ("C", 10.0)])
        self.assertEqual(scores["A"], 0.0)
        self.assertEqual(scores["B"], 50.0)
        self.assertEqual(scores["C"], 100.0)


class DynamicRsiThresholdTests(unittest.TestCase):
    def test_static_when_disabled(self):
        cfg = {"dynamic_rsi_enabled": False}
        self.assertEqual(dynamic_rsi_threshold(90.0, 35.0, cfg), 35.0)

    def test_anchor_score(self):
        cfg = {
            "dynamic_rsi_enabled": True,
            "dynamic_rsi_base": 35,
            "dynamic_rsi_anchor_score": 70,
            "dynamic_rsi_slope": 0.5,
            "dynamic_rsi_max": 55,
        }
        self.assertEqual(dynamic_rsi_threshold(70.0, 35.0, cfg), 35.0)

    def test_strong_stock_higher_rsi_allowed(self):
        cfg = {
            "dynamic_rsi_enabled": True,
            "dynamic_rsi_base": 35,
            "dynamic_rsi_anchor_score": 70,
            "dynamic_rsi_slope": 0.5,
            "dynamic_rsi_max": 55,
        }
        # RS 100 → 35 + (100-70)*0.5 = 50
        self.assertEqual(dynamic_rsi_threshold(100.0, 35.0, cfg), 50.0)

    def test_capped_at_max(self):
        cfg = {
            "dynamic_rsi_enabled": True,
            "dynamic_rsi_base": 35,
            "dynamic_rsi_anchor_score": 70,
            "dynamic_rsi_slope": 0.5,
            "dynamic_rsi_max": 45,
        }
        self.assertEqual(dynamic_rsi_threshold(100.0, 35.0, cfg), 45.0)

    def test_none_falls_back_to_static(self):
        cfg = {"dynamic_rsi_enabled": True}
        self.assertEqual(dynamic_rsi_threshold(None, 35.0, cfg), 35.0)


class ApplyRsFilterTests(unittest.TestCase):
    def test_disabled(self):
        rows = [{"ticker": "A", "rs_score": 10}]
        cfg = {"filter_enabled": False}
        self.assertEqual(apply_rs_filter(rows, cfg), rows)

    def test_enabled(self):
        rows = [
            {"ticker": "A", "rs_score": 80},
            {"ticker": "B", "rs_score": 60},
            {"ticker": "C", "rs_score": None},
        ]
        cfg = {"filter_enabled": True, "min_rs_score": 70}
        kept = apply_rs_filter(rows, cfg)
        self.assertEqual([r["ticker"] for r in kept], ["A"])


class IndexReturnPctTests(unittest.TestCase):
    def test_insufficient_data(self):
        with patch("src.rs_score.fetch_all", return_value=[{"date": "2026-01-01", "close": 100.0}]):
            with patch("src.rs_score.get_connection") as mock_conn:
                mock_conn.return_value.__enter__ = MagicMock(return_value=MagicMock())
                mock_conn.return_value.__exit__ = MagicMock(return_value=False)
                self.assertIsNone(index_return_pct("N225", date(2026, 6, 1), 126))

    def test_positive_return(self):
        rows = [{"date": f"2026-01-{i:02d}", "close": 100.0 + i} for i in range(1, 140)]
        with patch("src.rs_score.fetch_all", return_value=rows):
            with patch("src.rs_score.get_connection") as mock_conn:
                mock_conn.return_value.__enter__ = MagicMock(return_value=MagicMock())
                mock_conn.return_value.__exit__ = MagicMock(return_value=False)
                ret = index_return_pct("N225", date(2026, 6, 1), 126)
        self.assertIsNotNone(ret)
        self.assertGreater(ret, 0)


class UpdateRsScoresTests(unittest.TestCase):
    @patch("src.rs_score.get_rs_score_config")
    @patch("src.rs_score.index_return_pct", return_value=5.0)
    @patch("src.rs_score.fetch_all")
    @patch("src.rs_score.get_connection")
    def test_updates_rows(self, mock_conn, mock_fetch_all, _mock_index, mock_cfg):
        mock_cfg.return_value = {"enabled": True, "lookback_days": 126}
        mock_fetch_all.return_value = [
            {"ticker": "A", "return_126d": 20.0},
            {"ticker": "B", "return_126d": 10.0},
            {"ticker": "C", "return_126d": 0.0},
        ]
        mock_db = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        count = update_rs_scores("JP", date(2026, 6, 1))
        self.assertEqual(count, 3)
        self.assertEqual(mock_db.execute.call_count, 3)


if __name__ == "__main__":
    unittest.main()
