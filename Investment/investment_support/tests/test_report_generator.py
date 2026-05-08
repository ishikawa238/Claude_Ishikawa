"""
report_generator モジュールのテスト

テスト対象:
- _build_blocks(): Slack メッセージブロック生成
- aggregate_system_stats(): システム運用統計の集計
"""

import unittest
from unittest.mock import patch


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
    "jquants": {"earnings_alert_days_warning": "7", "earnings_alert_days_critical": "3"},
    "report_server": {"host": "127.0.0.1", "port": "5100"},
}

with patch("src.config.CONFIG", _CONFIG_MOCK):
    from src.report_generator import _append_system_stats, _build_blocks


class BuildBlocksTests(unittest.TestCase):
    """_build_blocks() のテスト"""

    def test_no_trades_no_sys_stats(self):
        """トレードなし・システム統計なしの場合"""
        stats = {
            "year": 2026, "month": 4,
            "total_trades": 0, "win": 0, "loss": 0,
            "win_rate": None, "avg_pnl_pct": None,
            "total_pnl_amount": None,
            "best_trade": None, "worst_trade": None,
            "trades": [],
        }
        blocks = _build_blocks(stats)
        texts = _extract_texts(blocks)
        self.assertTrue(any("決済済みトレードはありません" in t for t in texts))

    def test_with_trades(self):
        """トレードありの場合にサマリーが含まれる"""
        stats = {
            "year": 2026, "month": 4,
            "total_trades": 2, "win": 1, "loss": 1,
            "win_rate": 50.0, "avg_pnl_pct": 2.5,
            "total_pnl_amount": 5000,
            "best_trade": {"ticker": "7203.TSE", "pnl_pct": 10.0},
            "worst_trade": {"ticker": "9984.TSE", "pnl_pct": -5.0},
            "trades": [
                {"actual_exit_date": "2026-04-15", "ticker": "7203.TSE",
                 "pnl_pct": 10.0, "exit_reason": "take_profit"},
                {"actual_exit_date": "2026-04-20", "ticker": "9984.TSE",
                 "pnl_pct": -5.0, "exit_reason": "stop_loss"},
            ],
        }
        blocks = _build_blocks(stats)
        texts = _extract_texts(blocks)
        self.assertTrue(any("取引件数" in t for t in texts))
        self.assertTrue(any("勝率" in t for t in texts))
        self.assertTrue(any("決済明細" in t for t in texts))

    def test_with_sys_stats(self):
        """システム統計が含まれる場合"""
        stats = {
            "year": 2026, "month": 4,
            "total_trades": 0, "win": 0, "loss": 0,
            "win_rate": None, "avg_pnl_pct": None,
            "total_pnl_amount": None,
            "best_trade": None, "worst_trade": None,
            "trades": [],
        }
        sys_stats = {
            "caution_days": 5,
            "full_caution_days": 2,
            "normal_days": 15,
            "signals_suppressed": 8,
            "signals_total": 12,
            "pattern_a_candidates": 7,
            "pattern_b_candidates": 5,
            "stop_loss_count": 3,
            "batch_runs": 44,
            "batch_failures": 1,
        }
        blocks = _build_blocks(stats, sys_stats)
        texts = _extract_texts(blocks)
        self.assertTrue(any("システム運用統計" in t for t in texts))
        self.assertTrue(any("市場環境" in t for t in texts))
        self.assertTrue(any("シグナル統計" in t for t in texts))
        self.assertTrue(any("損切り実行" in t for t in texts))


class AppendSystemStatsTests(unittest.TestCase):
    """_append_system_stats() のテスト"""

    def test_guide_hints_shown(self):
        """改善ヒントが条件に応じて表示される"""
        blocks = []
        ss = {
            "caution_days": 10,
            "full_caution_days": 5,
            "normal_days": 7,
            "signals_suppressed": 15,
            "signals_total": 3,
            "pattern_a_candidates": 2,
            "pattern_b_candidates": 1,
            "stop_loss_count": 0,
            "batch_runs": 44,
            "batch_failures": 2,
        }
        _append_system_stats(blocks, ss)
        texts = _extract_texts(blocks)
        self.assertTrue(any("改善のヒント" in t for t in texts))
        self.assertTrue(any("警戒モードで15件停止" in t for t in texts))
        self.assertTrue(any("バッチ失敗2回" in t for t in texts))

    def test_no_hints_when_healthy(self):
        """正常運用時はヒントが表示されない"""
        blocks = []
        ss = {
            "caution_days": 0,
            "full_caution_days": 0,
            "normal_days": 22,
            "signals_suppressed": 0,
            "signals_total": 10,
            "pattern_a_candidates": 6,
            "pattern_b_candidates": 4,
            "stop_loss_count": 1,
            "batch_runs": 44,
            "batch_failures": 0,
        }
        _append_system_stats(blocks, ss)
        texts = _extract_texts(blocks)
        self.assertFalse(any("改善のヒント" in t for t in texts))


def _extract_texts(blocks: list) -> list[str]:
    """ブロックリストからテキスト文字列を再帰的に抽出する。"""
    texts = []
    for b in blocks:
        if "text" in b:
            t = b["text"]
            if isinstance(t, dict):
                texts.append(t.get("text", ""))
            elif isinstance(t, str):
                texts.append(t)
        if "fields" in b:
            for f in b["fields"]:
                if isinstance(f, dict):
                    texts.append(f.get("text", ""))
    return texts


if __name__ == "__main__":
    unittest.main()
