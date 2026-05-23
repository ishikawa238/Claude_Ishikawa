"""
backtester モジュールのテスト

テスト対象:
- summarize_results(): 統計計算の正しさ
- format_summary(): 出力フォーマット
- _calc_max_consecutive_losses(): 最大連敗数
- _calc_max_drawdown(): 最大ドローダウン
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))


_CONFIG_MOCK = {
    "signal": {
        "atr_stop_multiplier": "2.0",
        "risk_reward_ratio": "2.0",
        "stop_loss_warning_min_pct": "1.0",
        "stop_loss_warning_max_pct": "15.0",
        "pattern_b_lookback_days": "5",
    },
    "capital": {
        "total_capital_jpy": "200000",
        "total_capital_usd": "1500",
        "max_risk_per_trade_pct": "2",
        "max_concurrent_holdings": "3",
        "default_stop_loss_pct": "8",
    },
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
    "slack": {"webhook_url": "https://XXXXX"},
}

with patch("src.config.CONFIG", _CONFIG_MOCK):
    from src.backtester import (
        _calc_max_consecutive_losses,
        _calc_max_drawdown,
        format_summary,
        summarize_results,
    )


def _make_trade(pnl_pct, pnl_amount=0, pattern="A", exit_date="2026-01-01", **kwargs):
    """テスト用トレード辞書を生成する。"""
    base = {
        "ticker": "7203.TSE",
        "market": "JP",
        "pattern": pattern,
        "signal_date": "2025-12-28",
        "entry_date": "2025-12-29",
        "entry_price": 1000.0,
        "exit_date": exit_date,
        "exit_price": 1000.0 * (1 + pnl_pct / 100),
        "exit_reason": "take_profit" if pnl_pct > 0 else "stop_loss",
        "position_size": 50000.0,
        "pnl_amount": pnl_amount,
        "pnl_pct": pnl_pct,
    }
    base.update(kwargs)
    return base


class SummarizeResultsTests(unittest.TestCase):
    """summarize_results() のテスト"""

    def test_empty_trades(self):
        """トレードがない場合はゼロ値を返す"""
        result = summarize_results([])
        self.assertEqual(result["total_trades"], 0)
        self.assertEqual(result["win_rate"], 0.0)
        self.assertEqual(result["expected_value_pct"], 0.0)
        self.assertEqual(result["pattern_stats"], {})

    def test_all_wins(self):
        """全勝の場合"""
        trades = [
            _make_trade(pnl_pct=5.0, pnl_amount=2500),
            _make_trade(pnl_pct=10.0, pnl_amount=5000),
        ]
        result = summarize_results(trades)
        self.assertEqual(result["total_trades"], 2)
        self.assertEqual(result["win_rate"], 100.0)
        self.assertEqual(result["avg_profit_pct"], 7.5)
        self.assertEqual(result["avg_loss_pct"], 0.0)
        self.assertEqual(result["max_consecutive_losses"], 0)

    def test_all_losses(self):
        """全敗の場合"""
        trades = [
            _make_trade(pnl_pct=-3.0, pnl_amount=-1500),
            _make_trade(pnl_pct=-5.0, pnl_amount=-2500, exit_date="2026-01-02"),
        ]
        result = summarize_results(trades)
        self.assertEqual(result["win_rate"], 0.0)
        self.assertEqual(result["avg_profit_pct"], 0.0)
        self.assertEqual(result["avg_loss_pct"], -4.0)
        self.assertEqual(result["max_consecutive_losses"], 2)

    def test_mixed_trades(self):
        """勝ち負け混在の場合"""
        trades = [
            _make_trade(pnl_pct=10.0, pnl_amount=5000, exit_date="2026-01-01"),
            _make_trade(pnl_pct=-5.0, pnl_amount=-2500, exit_date="2026-01-02"),
            _make_trade(pnl_pct=8.0, pnl_amount=4000, exit_date="2026-01-03"),
            _make_trade(pnl_pct=-3.0, pnl_amount=-1500, exit_date="2026-01-04"),
        ]
        result = summarize_results(trades)
        self.assertEqual(result["total_trades"], 4)
        self.assertEqual(result["win_rate"], 50.0)
        self.assertEqual(result["avg_profit_pct"], 9.0)   # (10+8)/2
        self.assertEqual(result["avg_loss_pct"], -4.0)     # (-5-3)/2
        # 期待値 = 0.5 * 9.0 + 0.5 * (-4.0) = 2.5
        self.assertEqual(result["expected_value_pct"], 2.5)
        self.assertEqual(result["total_pnl"], 5000.0)

    def test_pattern_stats(self):
        """パターン別統計が正しく計算される"""
        trades = [
            _make_trade(pnl_pct=10.0, pnl_amount=5000, pattern="A"),
            _make_trade(pnl_pct=-5.0, pnl_amount=-2500, pattern="A"),
            _make_trade(pnl_pct=6.0, pnl_amount=3000, pattern="B"),
        ]
        result = summarize_results(trades)
        self.assertIn("A", result["pattern_stats"])
        self.assertIn("B", result["pattern_stats"])
        self.assertEqual(result["pattern_stats"]["A"]["total"], 2)
        self.assertEqual(result["pattern_stats"]["A"]["win_rate"], 50.0)
        self.assertEqual(result["pattern_stats"]["B"]["total"], 1)
        self.assertEqual(result["pattern_stats"]["B"]["win_rate"], 100.0)


class MaxConsecutiveLossesTests(unittest.TestCase):
    """_calc_max_consecutive_losses() のテスト"""

    def test_no_losses(self):
        trades = [_make_trade(pnl_pct=5.0)]
        self.assertEqual(_calc_max_consecutive_losses(trades), 0)

    def test_consecutive_losses(self):
        trades = [
            _make_trade(pnl_pct=5.0, exit_date="2026-01-01"),
            _make_trade(pnl_pct=-3.0, exit_date="2026-01-02"),
            _make_trade(pnl_pct=-2.0, exit_date="2026-01-03"),
            _make_trade(pnl_pct=-4.0, exit_date="2026-01-04"),
            _make_trade(pnl_pct=1.0, exit_date="2026-01-05"),
            _make_trade(pnl_pct=-1.0, exit_date="2026-01-06"),
        ]
        self.assertEqual(_calc_max_consecutive_losses(trades), 3)

    def test_empty_trades(self):
        self.assertEqual(_calc_max_consecutive_losses([]), 0)


class MaxDrawdownTests(unittest.TestCase):
    """_calc_max_drawdown() のテスト"""

    def test_no_drawdown(self):
        """常に利益なら DD = 0"""
        trades = [
            _make_trade(pnl_pct=5.0, pnl_amount=5000, exit_date="2026-01-01"),
            _make_trade(pnl_pct=3.0, pnl_amount=3000, exit_date="2026-01-02"),
        ]
        dd = _calc_max_drawdown(trades, 100000)
        self.assertEqual(dd, 0.0)

    def test_drawdown_calculation(self):
        """ドローダウンが正しく計算される"""
        trades = [
            _make_trade(pnl_pct=10.0, pnl_amount=10000, exit_date="2026-01-01"),
            _make_trade(pnl_pct=-5.0, pnl_amount=-5000, exit_date="2026-01-02"),
            _make_trade(pnl_pct=-3.0, pnl_amount=-3000, exit_date="2026-01-03"),
        ]
        # peak = 110000, valley = 102000, DD = 8000/110000 ≈ 7.27%
        dd = _calc_max_drawdown(trades, 100000)
        self.assertAlmostEqual(dd, 8000 / 110000 * 100, places=1)

    def test_zero_capital(self):
        """初期資金0の場合は DD = 0"""
        trades = [_make_trade(pnl_pct=-5.0, pnl_amount=-5000)]
        self.assertEqual(_calc_max_drawdown(trades, 0), 0.0)


class FormatSummaryTests(unittest.TestCase):
    """format_summary() のテスト"""

    def test_empty_summary(self):
        """トレードなしの場合のフォーマット"""
        summary = summarize_results([])
        output = format_summary(summary)
        self.assertIn("トレードなし", output)

    def test_with_trades(self):
        """トレードありの場合のフォーマット"""
        trades = [
            _make_trade(pnl_pct=10.0, pnl_amount=5000, pattern="A"),
            _make_trade(pnl_pct=-5.0, pnl_amount=-2500, pattern="B"),
        ]
        summary = summarize_results(trades)
        output = format_summary(summary, initial_capital=200000)
        self.assertIn("勝率", output)
        self.assertIn("期待値", output)
        self.assertIn("総リターン", output)
        self.assertIn("モメンタムブレイクアウト", output)
        self.assertIn("押し目買い", output)


if __name__ == "__main__":
    unittest.main()
