"""score_trend モジュールのテスト"""

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT.parent.parent))
sys.path.insert(0, str(_PROJECT_ROOT))

from src.score_trend import (
    SCORE_TREND_THRESHOLD,
    compute_score_trend,
    format_score_trend_badge,
    score_trend_label,
)


class ComputeScoreTrendTests(unittest.TestCase):
    def test_up_trend(self):
        r = compute_score_trend([60.0, 62.0, 65.0, 70.0, 75.0])
        self.assertEqual(r["direction"], "up")
        self.assertGreaterEqual(r["delta"], SCORE_TREND_THRESHOLD)

    def test_down_trend(self):
        r = compute_score_trend([80.0, 78.0, 75.0, 72.0, 68.0])
        self.assertEqual(r["direction"], "down")
        self.assertLessEqual(r["delta"], -SCORE_TREND_THRESHOLD)

    def test_flat_trend(self):
        r = compute_score_trend([70.0, 71.0, 70.5, 71.0, 71.5])
        self.assertEqual(r["direction"], "flat")

    def test_unknown_with_single_point(self):
        r = compute_score_trend([72.0])
        self.assertEqual(r["direction"], "unknown")

    def test_compare_days_uses_lookback(self):
        r = compute_score_trend([50.0, 55.0, 60.0, 65.0, 70.0, 75.0, 80.0])
        self.assertEqual(r["compare_days"], 5)
        self.assertEqual(r["previous"], 55.0)
        self.assertEqual(r["latest"], 80.0)


class FormatBadgeTests(unittest.TestCase):
    def test_badge_contains_arrow(self):
        html = format_score_trend_badge(
            {"direction": "up", "delta": 4.0, "compare_days": 5},
        )
        self.assertIn("score-trend-up", html)
        self.assertIn("↑", html)

    def test_unknown_returns_empty(self):
        self.assertEqual(format_score_trend_badge({"direction": "unknown"}), "")

    def test_labels(self):
        self.assertEqual(score_trend_label("up"), "上昇")
        self.assertEqual(score_trend_label("down"), "下降")


if __name__ == "__main__":
    unittest.main()
