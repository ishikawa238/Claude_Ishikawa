"""
rule_backtest のシミュレータと集計のテスト

テスト対象:
- PortfolioSimulator: 翌寄付の約定、損切り（場中・終値）、exit_simple の決済、保有枠・資金・売買単位、
  運用停止ルール、市場環境、価格の不連続、期間終了時の手仕舞い、手数料・スリッページ
- 日米合算: 処理順、保有枠と運用停止ルールの共有、市場ごとの資金、円換算
- report: 最大ドローダウン、年率、分割決済の集約、年別・戦略別の集計、ファイル出力
"""

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.rule_backtest.report import (
    aggregate_positions,
    blended_benchmark_pct,
    cagr_pct,
    format_by_strategy,
    format_holdings_comparison,
    format_summary,
    format_yearly,
    max_drawdown,
    monthly_returns,
    summarize,
    write_outputs,
    yearly_stats,
)
from src.rule_backtest.settings import BacktestSettings
from src.rule_backtest.simulator import PortfolioSimulator


def _calendar(n: int, start: date = date(2026, 1, 5)) -> list[str]:
    out = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


class FakeFeed:
    """bars: {ticker: {k: (open, high, low, close)}} / signals: {k: [ticker, ...]}"""

    def __init__(self, n_days, bars, signals=None, modes=None, jumps=None, stop_pct=5.0, calendar=None):
        self.calendar = calendar or _calendar(n_days)
        self.bars = bars
        self._signals = signals or {}
        self.modes = modes or {}
        self.jumps = jumps or set()
        self.stop_pct = stop_pct
        self.signal_calls = []

    def bar(self, ticker, k):
        b = self.bars.get(ticker, {}).get(k)
        return None if b is None else dict(zip(("open", "high", "low", "close"), b))

    def price_jump(self, ticker, k):
        return (ticker, k) in self.jumps

    def exit_snapshot(self, ticker, k):
        return {"ema25": 1.0, "ema75": 1.0, "ma25": 1.0, "rsi14": 50.0, "resistance_level": None}

    def market_mode(self, k):
        return self.modes.get(k, "normal")

    def signals(self, k):
        self.signal_calls.append(k)
        return [{"ticker": t, "strategy": "swing_jp"} for t in self._signals.get(k, [])]

    def entry_set(self, signal, open_count):
        return {"stop_loss_pct": self.stop_pct}


def _flat(n, price=100.0):
    return {k: (price, price, price, price) for k in range(n)}


def _settings(calendar, markets=("JP",), capital=None, **kw) -> BacktestSettings:
    values = dict(
        markets=markets, start=calendar[0], end=calendar[-1],
        capital=capital or {"JP": 100_000, "US": 1_000},
        max_holdings=2, risk_pct=0.02, usdjpy=150.0,
        commission_pct=0.0, slippage_pct=0.0, risk_control=False,
    )
    values.update(kw)
    return BacktestSettings(**values)


def _run(feed, **kw):
    with patch("src.rule_backtest.simulator.is_exit_simple_enabled", return_value=False):
        return PortfolioSimulator(_settings(feed.calendar, **kw), {"JP": feed}).run()


def _run_combined(jp, us, **kw):
    settings = _settings(jp.calendar, markets=("JP", "US"), **kw)
    with patch("src.rule_backtest.simulator.is_exit_simple_enabled", return_value=False):
        return PortfolioSimulator(settings, {"JP": jp, "US": us}).run()


class SettingsTests(unittest.TestCase):
    def test_markets_are_normalized_and_converted(self):
        s = _settings(_calendar(2), markets=("US", "JP"))
        self.assertEqual(s.markets, ("JP", "US"))
        self.assertEqual(s.currency, "JPY")
        self.assertEqual(s.fx("US"), 150.0)
        self.assertAlmostEqual(s.initial_capital, 100_000 + 1_000 * 150)

    def test_single_us_market_uses_dollars(self):
        s = _settings(_calendar(2), markets=("US",))
        self.assertEqual(s.currency, "USD")
        self.assertEqual(s.fx("US"), 1.0)
        self.assertEqual(s.initial_capital, 1_000)

    def test_missing_capital_is_rejected(self):
        with self.assertRaises(ValueError):
            _settings(_calendar(2), markets=("JP", "US"), capital={"JP": 100_000})


class EntryTests(unittest.TestCase):
    def test_signal_is_filled_at_next_open(self):
        bars = _flat(4)
        bars[1] = (101.0, 102.0, 100.0, 101.0)
        feed = FakeFeed(4, {"A": bars}, signals={0: ["A"]})
        result = _run(feed)
        self.assertEqual(len(result.trades), 1)
        t = result.trades[0]
        self.assertEqual(t["market"], "JP")
        self.assertEqual(t["signal_date"], feed.calendar[0])
        self.assertEqual(t["entry_date"], feed.calendar[1])
        self.assertEqual(t["entry_price"], 101.0)
        # 100,000 × 2% ÷ 5% = 40,000（上限 100,000 ÷ 2 = 50,000）→ 40,000 ÷ 101 = 396 株
        self.assertEqual(t["initial_shares"], 396)
        self.assertAlmostEqual(t["stop_loss_price"], 101.0 * 0.95)
        self.assertEqual(t["exit_reason"], "end_of_test")
        self.assertEqual(result.counters["JP"]["entries"], 1)

    def test_no_signals_on_last_day(self):
        feed = FakeFeed(3, {"A": _flat(3)}, signals={2: ["A"]})
        result = _run(feed)
        self.assertNotIn(2, feed.signal_calls)
        self.assertEqual(result.trades, [])

    def test_slots_limit_entries(self):
        feed = FakeFeed(3, {t: _flat(3) for t in "ABC"}, signals={0: ["A", "B", "C"]})
        result = _run(feed, max_holdings=2)
        self.assertEqual(result.counters["JP"]["entries"], 2)
        self.assertEqual(result.counters["JP"]["skipped_slots"], 1)

    def test_held_ticker_is_skipped(self):
        feed = FakeFeed(4, {"A": _flat(4)}, signals={0: ["A"], 1: ["A"]})
        result = _run(feed)
        self.assertEqual(result.counters["JP"]["entries"], 1)
        self.assertEqual(result.counters["JP"]["skipped_held"], 1)

    def test_amount_capped_by_cash_and_commission(self):
        feed = FakeFeed(3, {"A": _flat(3)}, signals={0: ["A"]}, stop_pct=1.0)
        result = _run(feed, max_holdings=1, commission_pct=0.1)
        # 推奨額 = min(200,000, 100,000, 100,000 ÷ 1) → 手数料込みで現金に収まる 999 株
        self.assertEqual(result.trades[0]["initial_shares"], 999)
        self.assertGreaterEqual(min(r["cash_JP"] for r in result.daily), 0)

    def test_lot_size_rounds_down(self):
        feed = FakeFeed(3, {"A": _flat(3)}, signals={0: ["A"]})
        result = _run(feed, lot_size=100)
        self.assertEqual(result.trades[0]["initial_shares"], 400)

    def test_too_small_and_no_bar_are_counted(self):
        bars = {"A": {0: (100, 100, 100, 100), 2: (100, 100, 100, 100)}, "B": _flat(3, 90_000.0)}
        feed = FakeFeed(3, bars, signals={0: ["A", "B"]})
        result = _run(feed)
        self.assertEqual(result.counters["JP"]["skipped_no_bar"], 1)
        self.assertEqual(result.counters["JP"]["skipped_too_small"], 1)
        self.assertEqual(result.trades, [])

    def test_compound_sizing_uses_equity(self):
        bars = _flat(6)
        bars.update({k: (200.0, 200.0, 200.0, 200.0) for k in (2, 3, 4, 5)})

        def feed():
            return FakeFeed(6, {"A": bars, "B": _flat(6, 100.0)}, signals={0: ["A"], 3: ["B"]})

        fixed = {r["ticker"]: r["initial_shares"] for r in _run(feed(), sizing="fixed").trades}
        compound = {r["ticker"]: r["initial_shares"] for r in _run(feed(), sizing="compound").trades}
        self.assertEqual(fixed["B"], 400)
        self.assertGreater(compound["B"], fixed["B"])


class StopLossTests(unittest.TestCase):
    def _feed(self, day2, **kw):
        bars = _flat(4)
        bars[2] = day2
        return FakeFeed(4, {"A": bars}, signals={0: ["A"]}, **kw)

    def test_intraday_stop_fills_at_stop_price(self):
        result = _run(self._feed((98.0, 99.0, 94.0, 96.0)))
        t = result.trades[0]
        self.assertEqual(t["exit_reason"], "stop_loss")
        self.assertEqual(t["exit_date"], _calendar(4)[2])
        self.assertAlmostEqual(t["exit_price"], 95.0)
        self.assertAlmostEqual(t["r_multiple"], -1.0)

    def test_intraday_stop_gap_down_fills_at_open(self):
        result = _run(self._feed((90.0, 92.0, 89.0, 91.0)))
        self.assertAlmostEqual(result.trades[0]["exit_price"], 90.0)

    def test_stop_can_trigger_on_entry_day(self):
        bars = _flat(3)
        bars[1] = (100.0, 100.0, 94.0, 96.0)
        result = _run(FakeFeed(3, {"A": bars}, signals={0: ["A"]}))
        self.assertEqual(result.trades[0]["exit_date"], _calendar(3)[1])
        self.assertEqual(result.trades[0]["exit_reason"], "stop_loss")

    def test_close_mode_sells_next_open(self):
        bars = _flat(5)
        bars[2] = (98.0, 99.0, 94.0, 96.0)       # 安値は割れたが終値は損切り価格より上
        bars[3] = (96.0, 96.0, 93.0, 94.0)       # 終値で割れた
        bars[4] = (93.0, 93.0, 93.0, 93.0)
        result = _run(FakeFeed(5, {"A": bars}, signals={0: ["A"]}), stop_mode="close")
        t = result.trades[0]
        self.assertEqual(t["exit_reason"], "stop_loss")
        self.assertEqual(t["exit_date"], _calendar(5)[4])
        self.assertAlmostEqual(t["exit_price"], 93.0)


class ExitSimpleTests(unittest.TestCase):
    def _run(self, bars, evaluate, n=None, **kw):
        feed = FakeFeed(n or len(bars), {"A": bars}, signals={0: ["A"]})
        with patch("src.rule_backtest.simulator.is_exit_simple_enabled", return_value=True), \
                patch("src.rule_backtest.simulator.evaluate_exit", side_effect=evaluate) as mock_eval:
            result = PortfolioSimulator(_settings(feed.calendar, **kw), {"JP": feed}).run()
        return result, mock_eval

    def test_full_take_profit_sells_next_open(self):
        bars = _flat(5)
        bars[2] = (110.0, 112.0, 109.0, 111.0)
        bars[3] = (112.0, 112.0, 112.0, 112.0)

        def evaluate(ticker, entry, close, pnl, qty, days, snap, **kw):
            return {"label": "tp_full", "sell_qty": qty} if close >= 110 else None

        result, _ = self._run(bars, evaluate)
        t = result.trades[0]
        self.assertEqual(t["exit_reason"], "tp_full")
        self.assertEqual(t["exit_date"], _calendar(5)[3])
        self.assertAlmostEqual(t["exit_price"], 112.0)

    def test_partial_then_extend_exit(self):
        bars = _flat(7)
        bars[2] = (110.0, 112.0, 109.0, 111.0)
        bars[3] = (111.0, 111.0, 111.0, 111.0)
        bars[4] = (104.0, 104.0, 104.0, 104.0)
        bars[5] = (103.0, 103.0, 103.0, 103.0)
        states = []

        def evaluate(ticker, entry, close, pnl, qty, days, snap, state=None, **kw):
            states.append(state["extend_active"])
            if not state["extend_active"] and close >= 110:
                return {"label": "tp_partial", "sell_qty": qty // 2}
            if state["extend_active"] and close < 105:
                return {"label": "tp_extend_exit", "sell_qty": qty}
            return None

        result, _ = self._run(bars, evaluate)
        self.assertEqual([t["exit_reason"] for t in result.trades], ["tp_partial", "tp_extend_exit"])
        self.assertEqual([t["is_partial"] for t in result.trades], [True, False])
        self.assertEqual(result.trades[0]["shares"], 200)
        self.assertEqual(result.trades[1]["shares"], 200)
        self.assertEqual(result.trades[1]["exit_date"], _calendar(7)[5])
        self.assertIn(True, states)
        positions = aggregate_positions(result.trades)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["exit_reason"], "tp_extend_exit")

    def test_partial_respects_lot_size(self):
        bars = _flat(5)
        bars[2] = (110.0, 112.0, 109.0, 111.0)

        def evaluate(ticker, entry, close, pnl, qty, days, snap, state=None, **kw):
            if not state["extend_active"] and close >= 110:
                return {"label": "tp_partial", "sell_qty": 150}
            return None

        result, _ = self._run(bars, evaluate, lot_size=100)
        self.assertEqual(result.trades[0]["initial_shares"], 400)
        self.assertEqual(result.trades[0]["shares"], 100)

    def test_skipped_near_stop(self):
        bars = _flat(4)
        bars[2] = (96.0, 96.0, 96.0, 96.0)         # 損切り 95 まで約 1%（2% 以内は判定しない）

        _, mock_eval = self._run(bars, lambda *a, **kw: None)
        called_days = [c.args[5] for c in mock_eval.call_args_list]
        self.assertEqual(called_days, [0, 2])

    def test_holding_days_are_calendar_days(self):
        bars = _flat(8)
        _, mock_eval = self._run(bars, lambda *a, **kw: None)
        days = [c.args[5] for c in mock_eval.call_args_list]
        self.assertIn(7, days)        # 同じ曜日の翌週 = 7 暦日


class RiskAndMarketTests(unittest.TestCase):
    def test_caution_day_has_no_entries(self):
        feed = FakeFeed(3, {"A": _flat(3)}, signals={0: ["A"]}, modes={0: "caution"})
        result = _run(feed)
        self.assertEqual(result.trades, [])
        self.assertEqual(result.counters["JP"]["caution_days"], 1)
        self.assertNotIn(0, feed.signal_calls)
        self.assertEqual(result.daily[0]["mode_JP"], "caution")

    def _sim(self, markets=("JP",), **kw):
        feeds = {m: FakeFeed(3, {"A": _flat(3)}, signals={0: ["A"]}) for m in markets}
        cal = feeds[markets[0]].calendar
        sim = PortfolioSimulator(_settings(cal, markets=markets, risk_control=True, **kw), feeds)
        return sim, feeds

    def test_consecutive_losses_pause_entries(self):
        sim, feeds = self._sim(monthly_loss_stop_pct=0, max_consecutive_losses=3)
        d = feeds["JP"].calendar[0]
        sim.result.trades = [{"market": "JP", "pnl_pct": -1.0, "pnl_amount": -10.0, "exit_date": "2026-01-02"}] * 3
        self.assertEqual(sim._plan_entries("JP", 0, d), "paused")
        self.assertEqual(sim.pending_entries["JP"], [])
        self.assertEqual(sim.result.first_pause, {"date": d, "market": "JP", "reason": "consecutive_losses"})
        self.assertEqual(sim.result.counters["JP"]["paused_days"], 1)
        self.assertEqual(feeds["JP"].signal_calls, [])

    def test_win_resets_consecutive_losses(self):
        sim, feeds = self._sim(monthly_loss_stop_pct=0, max_consecutive_losses=3)
        loss = {"market": "JP", "pnl_pct": -1.0, "pnl_amount": -10.0, "exit_date": "2026-01-02"}
        win = {"market": "JP", "pnl_pct": 2.0, "pnl_amount": 20.0, "exit_date": "2026-01-02"}
        sim.result.trades = [loss, loss, loss, win, loss]
        self.assertIsNone(sim._pause_reason(feeds["JP"].calendar[0]))

    def test_monthly_loss_counts_current_month_only(self):
        sim, _ = self._sim(monthly_loss_stop_pct=6.0, max_consecutive_losses=0)
        sim.result.trades = [{"market": "JP", "pnl_pct": -5.0, "pnl_amount": -7_000.0, "exit_date": "2025-12-30"}]
        self.assertIsNone(sim._pause_reason("2026-01-05"))
        sim.result.trades.append({"market": "JP", "pnl_pct": -5.0, "pnl_amount": -6_000.0, "exit_date": "2026-01-05"})
        self.assertEqual(sim._pause_reason("2026-01-05"), "monthly_loss")

    def test_price_jump_sells_at_previous_close(self):
        bars = _flat(4)
        bars[2] = (30.0, 31.0, 29.0, 30.0)
        feed = FakeFeed(4, {"A": bars}, signals={0: ["A"]}, jumps={("A", 2)})
        t = _run(feed).trades[0]
        self.assertEqual(t["exit_reason"], "price_jump")
        self.assertAlmostEqual(t["exit_price"], 100.0)


class CombinedMarketTests(unittest.TestCase):
    def test_timeline_processes_jp_before_us_on_same_date(self):
        jp = FakeFeed(2, {})
        us = FakeFeed(0, {}, calendar=["2026-01-05", "2026-01-07"])
        sim = PortfolioSimulator(_settings(jp.calendar, markets=("JP", "US")), {"JP": jp, "US": us})
        self.assertEqual(
            sim._timeline(),
            [("2026-01-05", "JP", 0), ("2026-01-05", "US", 0), ("2026-01-06", "JP", 1), ("2026-01-07", "US", 1)],
        )

    def test_slots_are_shared_across_markets(self):
        jp = FakeFeed(3, {"A": _flat(3)}, signals={0: ["A"]})
        us = FakeFeed(3, {"X": _flat(3)}, signals={0: ["X"]})
        result = _run_combined(jp, us, max_holdings=1)
        self.assertEqual([t["ticker"] for t in result.trades], ["A"])
        self.assertEqual(result.counters["US"]["skipped_slots"], 1)

    def test_cash_is_per_market_and_equity_in_jpy(self):
        jp = FakeFeed(3, {"A": _flat(3)})
        us = FakeFeed(3, {"X": _flat(3)}, signals={0: ["X"]})
        result = _run_combined(jp, us)
        self.assertAlmostEqual(result.daily[0]["equity"], 100_000 + 1_000 * 150)
        # 1,000 × 2% ÷ 5% = 400 ドル（上限 1,000 ÷ 2 = 500）→ 4 株
        self.assertEqual(result.trades[0]["initial_shares"], 4)
        self.assertAlmostEqual(result.daily[1]["cash_US"], 600.0)
        self.assertAlmostEqual(result.daily[1]["cash_JP"], 100_000.0)
        self.assertAlmostEqual(result.daily[1]["equity"], 100_000 + 1_000 * 150)

    def test_pause_rules_are_shared_and_converted(self):
        jp = FakeFeed(3, {})
        us = FakeFeed(3, {})
        settings = _settings(jp.calendar, markets=("JP", "US"), risk_control=True,
                             monthly_loss_stop_pct=6.0, max_consecutive_losses=3)
        sim = PortfolioSimulator(settings, {"JP": jp, "US": us})
        d = jp.calendar[0]
        # 月間損失の停止ライン = (100,000 + 1,000 × 150) × 6% = 15,000 円
        sim.result.trades = [{"market": "US", "pnl_pct": 1.0, "pnl_amount": -99.0, "exit_date": d}]
        self.assertIsNone(sim._pause_reason(d))
        sim.result.trades = [{"market": "US", "pnl_pct": 1.0, "pnl_amount": -100.0, "exit_date": d}]
        self.assertEqual(sim._pause_reason(d), "monthly_loss")
        sim.result.trades = [{"market": "JP", "pnl_pct": -1.0, "pnl_amount": -1.0, "exit_date": d}] * 3
        self.assertEqual(sim._plan_entries("US", 0, d), "paused")


class CostAndEndTests(unittest.TestCase):
    def test_commission_and_slippage(self):
        bars = _flat(3)
        bars[2] = (100.0, 100.0, 100.0, 110.0)
        feed = FakeFeed(3, {"A": bars}, signals={0: ["A"]})
        result = _run(feed, commission_pct=0.1, slippage_pct=0.1)
        t = result.trades[0]
        buy = 100.0 * 1.001
        sell = 110.0 * 0.999
        shares = t["shares"]
        self.assertAlmostEqual(t["entry_price"], buy)
        self.assertAlmostEqual(t["exit_price"], sell)
        self.assertEqual(t["exit_reason"], "end_of_test")
        expected = (sell - buy) * shares - buy * shares * 0.001 - sell * shares * 0.001
        self.assertAlmostEqual(t["pnl_amount"], expected)
        self.assertAlmostEqual(result.daily[-1]["equity"], 100_000 + expected)
        self.assertEqual(result.daily[-1]["positions"], 0)

    def test_pending_exit_waits_for_bar(self):
        bars = {0: (100, 100, 100, 100), 1: (100, 100, 100, 100), 2: (96, 96, 93, 94), 4: (92, 92, 92, 92)}
        feed = FakeFeed(5, {"A": bars}, signals={0: ["A"]})
        t = _run(feed, stop_mode="close").trades[0]
        self.assertEqual(t["exit_date"], _calendar(5)[4])
        self.assertAlmostEqual(t["exit_price"], 92.0)


class ReportTests(unittest.TestCase):
    def test_max_drawdown(self):
        daily = [{"date": f"2026-01-0{i + 1}", "equity": e} for i, e in enumerate([100, 120, 90, 130, 117])]
        dd, when = max_drawdown(daily)
        self.assertAlmostEqual(dd, 25.0)
        self.assertEqual(when, "2026-01-03")
        self.assertEqual(max_drawdown([]), (0.0, None))

    def test_cagr(self):
        daily = [{"date": "2024-01-01", "equity": 100.0}, {"date": "2026-01-01", "equity": 121.0}]
        self.assertAlmostEqual(cagr_pct(100.0, 121.0, daily), 10.0, places=1)
        self.assertIsNone(cagr_pct(100.0, 121.0, daily[:1]))

    def test_monthly_returns(self):
        daily = [
            {"date": "2026-01-05", "equity": 101.0},
            {"date": "2026-01-30", "equity": 110.0},
            {"date": "2026-02-27", "equity": 99.0},
        ]
        out = monthly_returns(100.0, daily)
        self.assertEqual([m["month"] for m in out], ["2026-01", "2026-02"])
        self.assertAlmostEqual(out[0]["return_pct"], 10.0)
        self.assertAlmostEqual(out[1]["return_pct"], -10.0)

    def test_aggregate_positions_uses_initial_cost_and_risk(self):
        base = {"position_id": 1, "market": "US", "ticker": "A", "strategy": "s", "entry_date": "2026-01-05",
                "entry_price": 100.0, "stop_loss_price": 95.0, "initial_shares": 10}
        trades = [
            dict(base, exit_date="2026-01-10", exit_reason="tp_partial", pnl_amount=50.0, holding_days=5),
            dict(base, exit_date="2026-01-20", exit_reason="tp_extend_exit", pnl_amount=30.0, holding_days=15),
        ]
        (pos,) = aggregate_positions(trades, fx=lambda m: 150.0 if m == "US" else 1.0)
        self.assertAlmostEqual(pos["pnl_amount"], 80.0)
        self.assertAlmostEqual(pos["pnl_base"], 12_000.0)
        self.assertAlmostEqual(pos["pnl_pct"], 8.0)
        self.assertAlmostEqual(pos["r_multiple"], 1.6)
        self.assertEqual(pos["holding_days"], 15)

    def test_yearly_stats(self):
        daily = [
            {"date": "2024-06-03", "equity": 90.0},
            {"date": "2024-12-30", "equity": 110.0},
            {"date": "2025-03-03", "equity": 99.0},
            {"date": "2025-12-30", "equity": 121.0},
        ]
        positions = [{"entry_date": "2025-02-03", "pnl_amount": 5.0, "pnl_base": 5.0, "pnl_pct": 5.0,
                      "r_multiple": 1.0, "holding_days": 10}]
        rows = yearly_stats(100.0, daily, positions, {"JP": {"yearly": {"2024": 12.0}}})
        self.assertEqual([r["year"] for r in rows], ["2024", "2025"])
        self.assertAlmostEqual(rows[0]["return_pct"], 10.0)
        self.assertAlmostEqual(rows[0]["max_drawdown_pct"], 10.0)    # 期首 100 → 90
        self.assertAlmostEqual(rows[1]["return_pct"], 10.0)
        self.assertAlmostEqual(rows[1]["max_drawdown_pct"], 10.0)    # 前年末 110 → 99
        self.assertEqual((rows[0]["entries"], rows[1]["entries"]), (0, 1))
        self.assertEqual(rows[0]["benchmarks"], {"JP": 12.0})
        self.assertEqual(rows[1]["benchmarks"], {"JP": None})

    def test_blended_benchmark_weights_by_capital(self):
        s = _settings(_calendar(2), markets=("JP", "US"))
        benchmarks = {"JP": {"return_pct": 10.0}, "US": {"return_pct": 20.0}}
        expected = (100_000 * 10.0 + 150_000 * 20.0) / 250_000
        self.assertAlmostEqual(blended_benchmark_pct(s, benchmarks), expected)
        self.assertIsNone(blended_benchmark_pct(s, {"JP": {"return_pct": 10.0}}))

    def test_summarize_and_outputs_for_combined_run(self):
        jp_bars = _flat(4)
        jp_bars[2] = (98.0, 99.0, 94.0, 96.0)
        jp = FakeFeed(4, {"A": jp_bars}, signals={0: ["A"]})
        us = FakeFeed(4, {"X": _flat(4, 100.0)}, signals={0: ["X"]})
        result = _run_combined(jp, us)
        benchmarks = {
            "JP": {"index": "N225", "return_pct": 5.0, "yearly": {"2026": 5.0}},
            "US": {"index": "SP500", "return_pct": 8.0, "yearly": {"2026": 8.0}},
        }
        summary = summarize(result, benchmarks=benchmarks)
        self.assertEqual(summary["currency"], "JPY")
        self.assertEqual(summary["positions"], 2)
        self.assertEqual(summary["exit_reasons"], {"stop_loss": 1, "end_of_test": 1})
        self.assertEqual([(g["market"], g["positions"]) for g in summary["by_strategy"]], [("JP", 1), ("US", 1)])
        self.assertAlmostEqual(summary["realized_pnl"], summary["total_pnl"])
        self.assertAlmostEqual(summary["excess_return_pct"],
                               summary["total_return_pct"] - summary["benchmark_return_pct"])
        for text in (format_summary(summary), format_by_strategy(summary), format_yearly(summary),
                     format_holdings_comparison([summary])):
            self.assertIsInstance(text, str)
        with tempfile.TemporaryDirectory() as tmp:
            out = write_outputs(result, summary, Path(tmp) / "run")
            for name in ("trades.csv", "equity.csv", "summary.json"):
                self.assertTrue((out / name).exists())


if __name__ == "__main__":
    unittest.main()
