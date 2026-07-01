"""breakout_detector モジュールのテスト"""

import unittest
from unittest.mock import patch

from src.breakout_detector import PATTERN_BREAKOUT, evaluate_breakout


def _base_tk(**overrides):
    base = {
        "ticker": "TEST",
        "market": "US",
        "close": 105.0,
        "high_60d_prior": 100.0,
        "rsi14": 55.0,
        "atr14": 2.0,
        "volume": 1_500_000,
        "vol_20d_avg": 1_000_000,
        "rs_score": 80.0,
    }
    base.update(overrides)
    return base


class EvaluateBreakoutTests(unittest.TestCase):
    def test_candidate_when_all_conditions_met(self):
        row = evaluate_breakout(_base_tk(), "2026-06-27", strategy="breakout_us")
        self.assertEqual(row["label"], "candidate")
        self.assertEqual(row["pattern"], PATTERN_BREAKOUT)
        self.assertEqual(row["strategy"], "breakout_us")
        self.assertEqual(row["cond_a_52w_high_near"], 1)
        self.assertEqual(row["cond_a_volume_surge"], 1)

    def test_skip_when_no_new_high(self):
        row = evaluate_breakout(
            _base_tk(close=99.0, high_60d_prior=100.0),
            "2026-06-27",
            strategy="breakout_us",
        )
        self.assertEqual(row["label"], "skip")
        self.assertIsNone(row["pattern"])

    def test_skip_when_low_volume(self):
        row = evaluate_breakout(
            _base_tk(volume=500_000, vol_20d_avg=1_000_000),
            "2026-06-27",
            strategy="breakout_us",
        )
        self.assertEqual(row["label"], "skip")

    def test_watch_when_high_and_volume_without_rs(self):
        with patch("src.breakout_detector.get_breakout_config") as mock_cfg:
            mock_cfg.return_value = {
                "volume_ratio_min": 1.5,
                "min_rs_score": 90,
            }
            row = evaluate_breakout(
                _base_tk(rs_score=50.0),
                "2026-06-27",
                strategy="breakout_us",
            )
        self.assertEqual(row["label"], "watch")


if __name__ == "__main__":
    unittest.main()
