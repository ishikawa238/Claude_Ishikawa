"""swing_detector モジュールのテスト"""

import unittest
from unittest.mock import MagicMock, patch

from src.swing_detector import evaluate_swing


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
        "support_level": 23.0,
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


class EvaluateSwingTests(unittest.TestCase):
    def test_candidate_when_all_conditions_met(self):
        row = evaluate_swing(_base_tk(), "2026-06-27")
        self.assertEqual(row["label"], "candidate")
        self.assertEqual(row["cond_c_weekly_uptrend"], 1)
        self.assertEqual(row["cond_c_rsi_oversold"], 1)
        self.assertEqual(row["cond_c_sar_reversal"], 1)
        self.assertEqual(row["macd_confirm_type"], "both")

    def test_macd_hist_only(self):
        row = evaluate_swing(
            _base_tk(
                macd_diff=-2.22,
                macd_dea=-1.33,
                macd_hist=-0.89,
                macd_diff_prev=-2.09,
                macd_dea_prev=-1.11,
                macd_hist_prev=-0.98,
            ),
            "2026-06-26",
        )
        self.assertEqual(row["macd_confirm_type"], "hist")
        self.assertEqual(row["cond_c_sar_reversal"], 1)

    def test_macd_gc_only(self):
        row = evaluate_swing(
            _base_tk(
                macd_diff=0.2,
                macd_dea=0.1,
                macd_hist=0.1,
                macd_diff_prev=-0.1,
                macd_dea_prev=0.05,
                macd_hist_prev=0.2,
            ),
            "2026-06-27",
        )
        self.assertEqual(row["macd_confirm_type"], "gc")
        self.assertEqual(row["cond_c_sar_reversal"], 1)

    def test_macd_neither(self):
        row = evaluate_swing(
            _base_tk(
                macd_diff=-0.5,
                macd_dea=-0.2,
                macd_hist=-0.3,
                macd_diff_prev=-0.4,
                macd_dea_prev=-0.2,
                macd_hist_prev=-0.2,
            ),
            "2026-06-27",
        )
        self.assertIsNone(row["macd_confirm_type"])
        self.assertEqual(row["cond_c_sar_reversal"], 0)

    def test_watch_when_uptrend_only(self):
        row = evaluate_swing(_base_tk(rsi14=45.0), "2026-06-27")
        self.assertEqual(row["label"], "watch")
        self.assertEqual(row["cond_c_weekly_uptrend"], 1)

    def test_skip_when_not_uptrend(self):
        row = evaluate_swing(_base_tk(ema25=20.0, ema75=25.0), "2026-06-27")
        self.assertEqual(row["label"], "skip")

    @patch("src.swing_detector.dynamic_rsi_threshold", return_value=50.0)
    def test_dynamic_rsi_allows_higher_rsi(self, _mock_dyn):
        row = evaluate_swing(_base_tk(rsi14=45.0, rs_score=100.0), "2026-06-27")
        self.assertEqual(row["cond_c_rsi_oversold"], 1)
        self.assertEqual(row["_rsi_threshold"], 50.0)

    @patch("src.swing_detector.fetch_all")
    @patch("src.swing_detector.get_connection")
    def test_rsi_lookback(self, mock_conn, mock_fetch_all):
        mock_conn.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)
        mock_fetch_all.return_value = [{"rsi14": 34.0}]
        row = evaluate_swing(_base_tk(rsi14=40.0), "2026-06-27")
        self.assertEqual(row["cond_c_rsi_oversold"], 1)


if __name__ == "__main__":
    unittest.main()
