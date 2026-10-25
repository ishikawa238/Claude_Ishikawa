"""
rule_backtest のデータ準備（指標・流動性・市場データ読み込み）のテスト

テスト対象:
- compute_indicators / return_series / support_resistance_at: calculate_snapshot と同じ値になること
- 稼働日数・価格の不連続・直近窓の最大値などのヘルパー
- load_market_data / run_rule_backtest: 一時 DB から読み込んで最後まで実行できること
"""

import math
import random
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.market_filter import screening_thresholds
from src.rule_backtest import BacktestSettings, compare_max_holdings, load_feeds, run_rule_backtest
from src.rule_backtest.indicators import (
    _active_days_series,
    compute_indicators,
    price_jump_flags,
    return_series,
    support_resistance_at,
)
from src.rule_backtest.market_data import (
    _ever_passes_liquidity,
    _prior_window_max,
    _recent_flags,
    _screen_ok,
    build_calendar,
    load_market_data,
)
from src.technical_calculator import calculate_snapshot

_SNAPSHOT_FIELDS = (
    "ema25", "ema75", "rsi14", "atr14", "ma25", "vol_20d_avg", "high_60d_prior",
    "macd_diff", "macd_dea", "macd_hist", "macd_diff_prev", "macd_dea_prev", "macd_hist_prev",
)


def _weekdays(start: date, n: int) -> list[str]:
    out = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _synthetic_bars(n: int, seed: int = 7, base: float = 1000.0):
    rng = random.Random(seed)
    dates = _weekdays(date(2024, 1, 1), n)
    closes, highs, lows, volumes = [], [], [], []
    price = base
    for i in range(n):
        price *= 1 + rng.uniform(-0.03, 0.03)
        closes.append(price)
        highs.append(price * (1 + rng.uniform(0, 0.02)))
        lows.append(price * (1 - rng.uniform(0, 0.02)))
        volumes.append(0.0 if i % 17 == 5 else float(rng.randint(300_000, 900_000)))
    return dates, highs, lows, closes, volumes


def _assert_same(test: unittest.TestCase, actual, expected, label: str) -> None:
    if expected is None:
        test.assertTrue(actual is None or math.isnan(actual), f"{label}: {actual} != None")
    else:
        test.assertIsNotNone(actual, label)
        test.assertAlmostEqual(float(actual), float(expected), places=9, msg=label)


class IndicatorEquivalenceTests(unittest.TestCase):
    """時系列で計算した指標が、日ごとの calculate_snapshot と一致する。"""

    @classmethod
    def setUpClass(cls):
        cls.dates, cls.highs, cls.lows, cls.closes, cls.volumes = _synthetic_bars(320)
        cls.ind = compute_indicators(cls.dates, cls.highs, cls.lows, cls.closes, cls.volumes)
        cls.returns = return_series(cls.closes)
        cls.prices = [
            {"date": d, "adj_close": c, "high": h, "low": low, "volume": v}
            for d, h, low, c, v in zip(cls.dates, cls.highs, cls.lows, cls.closes, cls.volumes)
        ]

    def test_matches_snapshot_on_prefixes(self):
        for i in (10, 20, 30, 40, 61, 80, 126, 127, 200, 319):
            snap = calculate_snapshot("T", date.fromisoformat(self.dates[i]), self.prices[:i + 1])
            for f in _SNAPSHOT_FIELDS:
                _assert_same(self, self.ind[f][i], snap[f], f"{f}[{i}]")
            _assert_same(self, self.returns[i], snap["return_126d"], f"return_126d[{i}]")

    def test_support_resistance_matches_snapshot(self):
        for i in (60, 150, 319):
            snap = calculate_snapshot("T", date.fromisoformat(self.dates[i]), self.prices[:i + 1])
            sup, res = support_resistance_at(self.closes, self.highs, self.lows, i)
            _assert_same(self, sup, snap["support_level"], f"support[{i}]")
            _assert_same(self, res, snap["resistance_level"], f"resistance[{i}]")

    def test_support_resistance_accepts_ndarray(self):
        arrays = [np.array(v) for v in (self.closes, self.highs, self.lows)]
        self.assertEqual(
            support_resistance_at(*arrays, 200),
            support_resistance_at(self.closes, self.highs, self.lows, 200),
        )


class IndicatorHelperTests(unittest.TestCase):
    def test_active_days_counts_volume_days_in_50_calendar_days(self):
        # market_filter._filter_inactive と同じく「対象日 - 50 日 ～ 対象日」（両端含む）で数える
        dates = ["2026-01-01", "2026-01-10", "2026-02-15", "2026-02-20", "2026-02-21"]
        volumes = [100, 0, 100, 100, 100]
        out = _active_days_series(dates, volumes, 50)
        self.assertEqual(out.tolist(), [1, 1, 2, 3, 3])
        self.assertEqual(_active_days_series(["2026-01-01", "2026-02-20"], [1, 1], 50).tolist(), [1, 2])
        self.assertEqual(_active_days_series(["2026-01-01", "2026-02-21"], [1, 1], 50).tolist(), [1, 1])

    def test_price_jump_flags(self):
        flags = price_jump_flags([100, 101, 30, 31, 90, 89])
        self.assertEqual(flags.tolist(), [False, False, True, False, True, False])


class MarketDataHelperTests(unittest.TestCase):
    def test_prior_window_max_excludes_today_and_old_days(self):
        dates = ["2026-01-01", "2026-01-02", "2026-01-05", "2026-04-04"]
        out = _prior_window_max(dates, [5.0, 1.0, 3.0, 2.0], 90)
        self.assertTrue(math.isinf(out[0]))           # 前日までのデータなし
        self.assertEqual(out[1:3].tolist(), [5.0, 5.0])
        self.assertEqual(out[3], 3.0)                 # 01-01 / 01-02 は 90 日より前
        self.assertTrue(math.isinf(_prior_window_max(["2026-01-01", "2026-06-01"], [5.0, 1.0], 90)[1]))

    def test_recent_flags_window(self):
        flags = np.array([False, True, False, False, False])
        with patch("src.rule_backtest.market_data._JUMP_LOOKBACK_BARS", 2):
            self.assertEqual(_recent_flags(flags).tolist(), [False, True, True, False, False])

    def test_build_calendar_drops_thin_days_and_out_of_range(self):
        from collections import Counter
        counts = Counter({"2026-01-05": 100, "2026-01-06": 40, "2026-01-07": 60, "2025-12-30": 100})
        self.assertEqual(build_calendar(counts, "2026-01-01", "2026-01-31"), ["2026-01-05", "2026-01-07"])
        self.assertEqual(build_calendar(Counter(), "2026-01-01", "2026-01-31"), [])

    def test_screen_ok_applies_each_threshold(self):
        th = {
            "min_price": 300.0, "max_price": 3000.0, "min_turnover": 1_000_000.0,
            "min_market_cap": 1e9, "allow_unknown_market_cap": True, "min_active_days": 2,
        }
        close = np.array([1000.0, 200.0, 4000.0, 1000.0, 1000.0, 1000.0])
        volume = np.array([2000.0, 9000.0, 1000.0, 500.0, 0.0, 2000.0])
        ind = {
            "ema25": np.array([1.0, 1.0, 1.0, 1.0, 1.0, np.nan]),
            "ema75": np.ones(6),
            "active_days": np.array([5, 5, 5, 5, 5, 5]),
        }
        self.assertEqual(
            _screen_ok(close, volume, ind, None, th).tolist(),
            [True, False, False, False, False, False],
        )
        self.assertFalse(_screen_ok(close, volume, ind, 100_000, th)[0])   # 時価総額 1億 < 10億
        th_strict = dict(th, allow_unknown_market_cap=False)
        self.assertFalse(_screen_ok(close, volume, ind, None, th_strict)[0])

    def test_ever_passes_liquidity_only_in_test_period(self):
        th = {"min_price": 300.0, "max_price": None, "min_turnover": 1_000_000.0}
        dates = ["2025-12-30", "2026-01-05"]
        self.assertTrue(_ever_passes_liquidity(dates, [1000, 1000], [0, 2000], th, "2026-01-01", "2026-01-31"))
        self.assertFalse(_ever_passes_liquidity(dates, [1000, 1000], [2000, 0], th, "2026-01-01", "2026-01-31"))

    def test_screening_thresholds_keys(self):
        th = screening_thresholds("JP")
        self.assertEqual(
            set(th),
            {"min_price", "max_price", "min_turnover", "min_market_cap",
             "allow_unknown_market_cap", "min_active_days"},
        )
        self.assertGreater(th["min_turnover"], 0)


class LoadMarketDataTests(unittest.TestCase):
    """一時 DB に最小限のテーブルを作り、読み込みからシミュレーションまで通す。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "bt.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE universe_master (
                ticker TEXT, name TEXT, market TEXT, shares_outstanding REAL,
                is_active INTEGER, is_excluded INTEGER
            );
            CREATE TABLE price_history (
                ticker TEXT, date TEXT, open REAL, high REAL, low REAL,
                adj_close REAL, volume REAL, is_holiday INTEGER
            );
            CREATE TABLE index_snapshot (index_code TEXT, date TEXT, close REAL);
            """
        )
        th = screening_thresholds("JP")
        price = max(th["min_price"] * 2, 1.0)
        volume = th["min_turnover"] / price * 3
        shares = th["min_market_cap"] / price * 10
        conn.executemany(
            "INSERT INTO universe_master VALUES (?, ?, 'JP', ?, 1, 0)",
            [("1111", "テストA", shares), ("2222", "テストB", None), ("9999", "除外", shares)],
        )
        conn.execute("UPDATE universe_master SET is_excluded = 1 WHERE ticker = '9999'")
        self.dates = _weekdays(date(2024, 1, 1), 360)
        for ticker, seed in (("1111", 1), ("2222", 2), ("9999", 3)):
            _, highs, lows, closes, _ = _synthetic_bars(360, seed=seed, base=price)
            conn.executemany(
                "INSERT INTO price_history VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                [(ticker, d, c, h, low, c, volume) for d, h, low, c in zip(self.dates, highs, lows, closes)],
            )
        for code in ("N225", "TOPIX"):
            conn.executemany(
                "INSERT INTO index_snapshot VALUES (?, ?, ?)",
                [(code, d, 30000 + i * 10) for i, d in enumerate(self.dates)],
            )
        conn.commit()
        conn.close()
        self.start = self.dates[300]
        self.end = self.dates[-1]

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_reads_active_universe_and_aligns_calendar(self):
        data = load_market_data("JP", self.start, self.end, db_path=self.db_path)
        self.assertEqual(data.calendar, self.dates[300:])
        self.assertEqual([t.ticker for t in data.tickers], ["1111", "2222"])
        self.assertEqual(data.loc.shape, (2, len(data.calendar)))
        self.assertEqual(int(data.loc[0, 0]), 300)
        self.assertTrue(data.screen.any())
        self.assertIn("N225", data.indexes)

    def _settings(self, **kw) -> BacktestSettings:
        values = dict(
            markets=("JP",), start=self.start, end=self.end,
            capital={"JP": 100_000}, max_holdings=3, risk_pct=0.02,
        )
        values.update(kw)
        return BacktestSettings(**values)

    def test_run_rule_backtest_completes(self):
        result, summary = run_rule_backtest(self._settings(), db_path=self.db_path)
        self.assertEqual(len(result.daily), len(self.dates[300:]))
        self.assertEqual(result.daily[-1]["positions"], 0)
        self.assertAlmostEqual(summary["final_equity"], result.daily[-1]["cash_JP"])
        self.assertIsNotNone(summary["benchmark_return_pct"])
        self.assertEqual(summary["benchmarks"]["JP"]["index"], "N225")
        self.assertEqual([y["year"] for y in summary["yearly"]], ["2025"])

    def test_compare_max_holdings_reuses_loaded_data(self):
        feeds = load_feeds(("JP",), self.start, self.end, db_path=self.db_path)
        summaries = compare_max_holdings(self._settings(), feeds, [1, 3])
        self.assertEqual([s["settings"]["max_holdings"] for s in summaries], [1, 3])


if __name__ == "__main__":
    unittest.main()
