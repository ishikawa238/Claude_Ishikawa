"""価格取得の事前フィルタ（universe_prefilter）のテスト。"""

import unittest
from datetime import date
from unittest.mock import patch

_CONFIG_MOCK = {
    "screening": {
        "jp_min_turnover": 500_000_000,
        "us_min_turnover": 20_000_000,
    },
    "universe_prefilter": {
        "enabled": True,
        "markets": ["US"],
        "lookback_days": 90,
        "min_turnover_ratio": 0.25,
        "full_refresh_weekday": 5,
    },
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
    "logging": {"log_dir": "/tmp", "log_level": "INFO"},
}

_THURSDAY = date(2026, 9, 24)  # weekday=3
_SATURDAY = date(2026, 9, 26)  # weekday=5
_UNIVERSE = [
    ("LIQUID", "US"),
    ("THIN", "US"),
    ("NEW", "US"),
    ("HELD", "US"),
]


with patch("src.config.CONFIG", _CONFIG_MOCK):
    from src import universe_prefilter


class PrefilterConfigTests(unittest.TestCase):

    def test_defaults_are_used_when_section_missing(self):
        with patch.object(universe_prefilter, "CONFIG", {"screening": {}}):
            cfg = universe_prefilter.get_prefilter_config()
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["markets"], ["US"])
        self.assertEqual(cfg["lookback_days"], 90)
        self.assertEqual(cfg["min_turnover_ratio"], 0.25)
        self.assertEqual(cfg["full_refresh_weekday"], 5)

    def test_user_values_override_defaults(self):
        with patch.object(
            universe_prefilter,
            "CONFIG",
            {"universe_prefilter": {"enabled": False, "lookback_days": 30}},
        ):
            cfg = universe_prefilter.get_prefilter_config()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["lookback_days"], 30)
        self.assertEqual(cfg["markets"], ["US"])

    def test_full_refresh_day_is_saturday_by_default(self):
        self.assertFalse(universe_prefilter.is_full_refresh_day(_THURSDAY))
        self.assertTrue(universe_prefilter.is_full_refresh_day(_SATURDAY))

    def test_full_refresh_can_be_disabled(self):
        cfg = {**universe_prefilter._DEFAULTS, "full_refresh_weekday": -1}
        self.assertFalse(universe_prefilter.is_full_refresh_day(_SATURDAY, cfg))


class SelectFetchTickersTests(unittest.TestCase):

    def _select(self, turnover, held=None, target=_THURSDAY, market="US", universe=None):
        held = held if held is not None else set()
        with patch.object(universe_prefilter, "_recent_max_turnover", return_value=turnover), \
             patch.object(universe_prefilter, "_held_tickers", return_value=held):
            return universe_prefilter.select_fetch_tickers(
                universe or list(_UNIVERSE), market, target,
            )

    def test_keeps_liquid_and_unknown_and_held(self):
        # 閾値 = 20_000_000 * 0.25 = 5_000_000
        selected = self._select(
            turnover={"LIQUID": 8_000_000, "THIN": 1_000_000, "HELD": 100},
            held={"HELD"},
        )
        self.assertEqual(
            selected,
            [("LIQUID", "US"), ("NEW", "US"), ("HELD", "US")],
        )

    def test_disabled_returns_all(self):
        cfg = {**_CONFIG_MOCK, "universe_prefilter": {**_CONFIG_MOCK["universe_prefilter"], "enabled": False}}
        with patch.object(universe_prefilter, "CONFIG", cfg):
            selected = self._select(turnover={"LIQUID": 1})
        self.assertEqual(selected, list(_UNIVERSE))

    def test_other_market_is_not_filtered(self):
        jp = [("7203.T", "JP"), ("9984.T", "JP")]
        selected = self._select(turnover={"7203.T": 1}, market="JP", universe=jp)
        self.assertEqual(selected, jp)

    def test_saturday_fetches_all(self):
        selected = self._select(
            turnover={"LIQUID": 1, "THIN": 1, "HELD": 1},
            target=_SATURDAY,
        )
        self.assertEqual(selected, list(_UNIVERSE))

    def test_preserves_input_order(self):
        universe = [("C", "US"), ("A", "US"), ("B", "US")]
        selected = self._select(
            turnover={"C": 9_000_000, "A": 9_000_000, "B": 9_000_000},
            universe=universe,
        )
        self.assertEqual([t for t, _ in selected], ["C", "A", "B"])

    def test_equal_to_threshold_is_kept(self):
        selected = self._select(turnover={"LIQUID": 5_000_000, "THIN": 4_999_999})
        tickers = {t for t, _ in selected}
        self.assertIn("LIQUID", tickers)
        self.assertNotIn("THIN", tickers)
        self.assertIn("NEW", tickers)


class HelperTests(unittest.TestCase):

    def test_min_turnover_uses_screening_and_ratio(self):
        cfg = universe_prefilter.get_prefilter_config()
        self.assertEqual(universe_prefilter._min_turnover("US", cfg), 5_000_000)
        self.assertEqual(universe_prefilter._min_turnover("JP", cfg), 125_000_000)

    def test_held_tickers_returns_empty_on_error(self):
        with patch("src.universe_prefilter.get_connection", side_effect=RuntimeError("no table")):
            self.assertEqual(universe_prefilter._held_tickers(), set())

    def test_recent_max_turnover_filters_to_requested_tickers(self):
        rows = [
            {"ticker": "AAA", "max_turnover": 10},
            {"ticker": "BBB", "max_turnover": None},
            {"ticker": "CCC", "max_turnover": 3},
        ]
        mock_conn = patch("src.universe_prefilter.get_connection")
        mock_fetch = patch("src.universe_prefilter.fetch_all", return_value=rows)
        with mock_conn, mock_fetch:
            out = universe_prefilter._recent_max_turnover({"AAA", "BBB"}, date(2026, 7, 1))
        self.assertEqual(out, {"AAA": 10.0, "BBB": 0.0})
        self.assertNotIn("CCC", out)


if __name__ == "__main__":
    unittest.main()
