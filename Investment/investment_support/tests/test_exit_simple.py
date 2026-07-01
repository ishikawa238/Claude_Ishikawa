"""シンプル出口ルールのテスト"""

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.exit_simple import (
    effective_target_pct,
    evaluate_exit,
    hold_conditions_met,
    resistance_reward_pct,
    resolve_exit_targets,
    target_price,
)


class TestEffectiveTargetPct(unittest.TestCase):
    def test_initial_period(self):
        self.assertEqual(effective_target_pct(0, _cfg()), 20.0)
        self.assertEqual(effective_target_pct(29, _cfg()), 20.0)

    def test_decay_schedule(self):
        cfg = _cfg()
        self.assertEqual(effective_target_pct(30, cfg), 15.0)
        self.assertEqual(effective_target_pct(60, cfg), 10.0)
        self.assertEqual(effective_target_pct(90, cfg), 5.0)
        self.assertEqual(effective_target_pct(200, cfg), 5.0)


class TestResistanceCap(unittest.TestCase):
    def test_cap_applies_when_below_time_decay(self):
        # entry 100, resistance 112 → cap price 112*0.985=110.32 → +10.32%
        pct = resistance_reward_pct(100.0, 112.0, _cfg())
        self.assertIsNotNone(pct)
        self.assertAlmostEqual(pct, 10.32, places=1)

    def test_cap_ignored_when_too_far(self):
        pct = resistance_reward_pct(100.0, 130.0, _cfg())
        self.assertIsNone(pct)

    def test_cap_ignored_when_too_close(self):
        pct = resistance_reward_pct(100.0, 105.0, _cfg())
        self.assertIsNone(pct)

    def test_resolve_binding_resistance(self):
        targets = resolve_exit_targets(100.0, 10, 112.0, _cfg())
        self.assertEqual(targets["target_binding"], "resistance")
        self.assertLess(targets["effective_target_pct"], targets["time_decay_target_pct"])
        self.assertEqual(targets["resistance_cap_active"], True)

    def test_resolve_binding_time_decay(self):
        targets = resolve_exit_targets(100.0, 10, 130.0, _cfg())
        self.assertEqual(targets["target_binding"], "time_decay")
        self.assertFalse(targets["resistance_cap_active"])


class TestHoldConditions(unittest.TestCase):
    def test_all_met(self):
        snap = {"ema25": 110, "ema75": 100, "ma25": 105, "rsi14": 55}
        ok, fails = hold_conditions_met(snap, 110.0, _cfg())
        self.assertTrue(ok)
        self.assertEqual(fails, [])

    def test_ema_fail(self):
        snap = {"ema25": 90, "ema75": 100, "ma25": 80, "rsi14": 50}
        ok, fails = hold_conditions_met(snap, 85.0, _cfg())
        self.assertFalse(ok)
        self.assertTrue(any("EMA" in f for f in fails))


class TestEvaluateExit(unittest.TestCase):
    def setUp(self):
        self._cfg_patcher = patch(
            "src.exit_simple.get_exit_simple_config", return_value=_cfg(),
        )
        self._en_patcher = patch(
            "src.exit_simple.is_exit_simple_enabled", return_value=True,
        )
        self._cfg_patcher.start()
        self._en_patcher.start()

    def tearDown(self):
        self._cfg_patcher.stop()
        self._en_patcher.stop()

    def test_full_take_profit_without_hold(self):
        snap = {"ema25": 90, "ema75": 100, "ma25": 95, "rsi14": 50}
        result = evaluate_exit(
            "TEST", 1000.0, 1200.0, 20.0, 1, 10, snap, persist_state=False,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["label"], "tp_full")
        self.assertEqual(result["sell_qty"], 1)

    def test_resistance_cap_triggers_before_twenty_pct(self):
        snap = {"ema25": 90, "ema75": 100, "ma25": 95, "rsi14": 50}
        # entry 100, resistance 112 → cap ~+10.32%
        result = evaluate_exit(
            "TEST", 100.0, 110.5, 10.5, 1, 10, snap,
            resistance_level=112.0, persist_state=False,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["label"], "tp_full")
        self.assertEqual(result["target_binding"], "resistance")
        self.assertIn("レジスタンス", result["reasons"][0])

    def test_partial_take_profit_with_hold(self):
        snap = {"ema25": 110, "ema75": 100, "ma25": 105, "rsi14": 55}
        result = evaluate_exit(
            "TEST", 1000.0, 1200.0, 20.0, 6, 10, snap, persist_state=False,
        )
        self.assertEqual(result["label"], "tp_partial")
        self.assertEqual(result["sell_qty"], 3)

    def test_extend_hold_single_share(self):
        snap = {"ema25": 110, "ema75": 100, "ma25": 105, "rsi14": 55}
        result = evaluate_exit(
            "TEST", 1000.0, 1200.0, 20.0, 1, 10, snap, persist_state=False,
        )
        self.assertEqual(result["label"], "tp_extend_hold")
        self.assertEqual(result["sell_qty"], 0)

    def test_stale_soft(self):
        result = evaluate_exit(
            "TEST", 1000.0, 1030.0, 3.0, 1, 95, None, persist_state=False,
        )
        self.assertEqual(result["label"], "stale_watch")

    def test_stale_hard(self):
        result = evaluate_exit(
            "TEST", 1000.0, 1050.0, 5.0, 1, 125, None, persist_state=False,
        )
        self.assertEqual(result["label"], "stale_exit")

    def test_target_price_calc(self):
        self.assertEqual(target_price(1000.0, 20.0), 1200.0)


class TestExitSimpleState(unittest.TestCase):
    def setUp(self):
        self._cfg_patcher = patch(
            "src.exit_simple.get_exit_simple_config", return_value=_cfg(),
        )
        self._en_patcher = patch(
            "src.exit_simple.is_exit_simple_enabled", return_value=True,
        )
        self._cfg_patcher.start()
        self._en_patcher.start()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE exit_simple_state (
                ticker TEXT PRIMARY KEY,
                extend_active INTEGER NOT NULL DEFAULT 0,
                partial_taken INTEGER NOT NULL DEFAULT 0,
                last_target_pct REAL,
                updated_at TEXT
            )
            """
        )
        self.conn.execute(
            "CREATE TABLE holdings (ticker TEXT, is_closed INTEGER)"
        )
        self.conn.execute("INSERT INTO holdings VALUES ('TEST', 0)")

    def tearDown(self):
        self._cfg_patcher.stop()
        self._en_patcher.stop()
        self.conn.close()

    def _patch_conn(self):
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            yield self.conn

        return patch("src.exit_simple.get_connection", _cm)

    def test_extend_exit_after_hold_breaks(self):
        self.conn.execute(
            "INSERT INTO exit_simple_state VALUES ('TEST', 1, 0, 20, 'now')"
        )
        snap = {"ema25": 90, "ema75": 100, "ma25": 95, "rsi14": 50}
        with self._patch_conn():
            result = evaluate_exit(
                "TEST", 1000.0, 1250.0, 25.0, 3, 20, snap, persist_state=True,
            )
        self.assertEqual(result["label"], "tp_extend_exit")
        self.assertEqual(result["sell_qty"], 3)
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM exit_simple_state WHERE ticker='TEST'"
        ).fetchone()
        self.assertEqual(row["c"], 0)


def _cfg() -> dict:
    return {
        "initial_target_pct": 20.0,
        "target_decay_pct": 5.0,
        "target_decay_every_days": 30,
        "target_floor_pct": 5.0,
        "rsi_max_for_hold": 70.0,
        "partial_sell_ratio": 0.5,
        "stale_days_soft": 90,
        "stale_min_pnl_pct": 5.0,
        "stale_days_hard": 120,
        "stale_hard_min_pnl_pct": 10.0,
        "resistance_cap": {
            "enabled": True,
            "buffer_pct": 1.5,
            "min_reward_pct": 8.0,
            "ignore_if_above_pct": 25.0,
        },
    }


if __name__ == "__main__":
    unittest.main()
