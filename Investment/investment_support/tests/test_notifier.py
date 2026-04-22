"""
notifier.py のブロック生成テスト

_entry_set_block() と _holding_block() の出力文字列に
期待するフィールドが含まれるか検証する。
"""

import unittest
from unittest.mock import patch, MagicMock


# DB・Webhook・設定ファイルに依存しないようモックを準備
_CONFIG_MOCK = {
    "slack": {"webhook_url": "https://XXXXX"},
    "jquants": {"earnings_alert_days_warning": "7", "earnings_alert_days_critical": "3"},
    "signal": {
        "pattern_a_rsi_min": "50", "pattern_a_rsi_max": "70",
        "pattern_b_rsi_min": "40", "pattern_b_rsi_max": "60",
    },
    "report_server": {"host": "127.0.0.1", "port": "5100"},
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
    "capital": {"total_capital_jpy": "1000000", "total_capital_usd": "10000",
                "max_risk_per_trade_pct": "2", "default_stop_loss_pct": "8"},
}

with patch("src.config.CONFIG", _CONFIG_MOCK):
    from src import notifier


def _make_entry_set(**kwargs):
    base = {
        "ticker": "AAPL",
        "name": "Apple Inc",
        "market": "US",
        "pattern": "A",
        "entry_price": 150.00,
        "stop_loss_price": 138.00,
        "stop_loss_pct": 8.0,
        "target_price": 174.00,
        "recommended_amount": 3750.0,
        "market_mode": "normal",
        "conditions_met": "[]",
        "_warn_narrow": False,
        "_warn_wide": False,
        "_rsi14": 55.0,
        "_vol_ratio": 2.3,
        "_rs_rank": 1,
    }
    base.update(kwargs)
    return base


def _make_holding(**kwargs):
    base = {
        "ticker": "NVDA",
        "name": "NVIDIA",
        "market": "US",
        "label": "hold",
        "current_price": 185.00,
        "entry_price": 170.00,
        "entry_date": "2026-04-01",
        "stop_loss_price": 159.51,
        "quantity": 10.0,
        "pnl_pct": 8.82,
        "pnl_amount": 150.0,
        "reasons": ["明確な売りシグナルなし"],
        "earnings_date": None,
        "earnings_days": None,
        "holding_days": 15,
        "target_price": 210.00,
    }
    base.update(kwargs)
    return base


class EntrySetBlockTests(unittest.TestCase):

    def _get_text(self, es, show_conditions=False):
        block = notifier._entry_set_block(es, show_conditions=show_conditions)
        return block["text"]["text"]

    def test_rank_shown_in_header(self):
        text = self._get_text(_make_entry_set(_rs_rank=1))
        self.assertIn("ランク1位", text)

    def test_rank_omitted_when_none(self):
        text = self._get_text(_make_entry_set(_rs_rank=None))
        self.assertNotIn("ランク", text)

    def test_rsi_and_vol_shown(self):
        text = self._get_text(_make_entry_set(_rsi14=55.0, _vol_ratio=2.3))
        self.assertIn("RSI: 55", text)
        self.assertIn("出来高: 2.3倍", text)

    def test_rsi_range_pattern_a(self):
        text = self._get_text(_make_entry_set(pattern="A", _rsi14=60.0))
        self.assertIn("50–70", text)

    def test_rsi_range_pattern_b(self):
        text = self._get_text(_make_entry_set(pattern="B", _rsi14=50.0))
        self.assertIn("40–60", text)

    def test_stop_and_target_on_one_line(self):
        text = self._get_text(_make_entry_set())
        self.assertIn("損切りライン", text)
        self.assertIn("→ 利確目安", text)
        self.assertIn("リスクリワード2:1", text)

    def test_stop_pct_has_minus_sign(self):
        text = self._get_text(_make_entry_set(stop_loss_pct=8.0))
        self.assertIn("-8.0%", text)

    def test_warn_narrow_shown(self):
        text = self._get_text(_make_entry_set(_warn_narrow=True, _warn_wide=False))
        self.assertIn("損切り幅が狭すぎます", text)

    def test_warn_wide_shown(self):
        text = self._get_text(_make_entry_set(_warn_narrow=False, _warn_wide=True))
        self.assertIn("損切り幅が広めです", text)

    def test_metrics_absent_when_none(self):
        text = self._get_text(_make_entry_set(_rsi14=None, _vol_ratio=None))
        self.assertNotIn("RSI", text)
        self.assertNotIn("出来高", text)


class HoldingBlockTests(unittest.TestCase):

    def _get_text(self, h, detail=False):
        block = notifier._holding_block(h, detail=detail)
        return block["text"]["text"]

    def test_holding_days_shown(self):
        text = self._get_text(_make_holding(holding_days=15))
        self.assertIn("保有: 15日目", text)

    def test_holding_days_absent_when_none(self):
        text = self._get_text(_make_holding(holding_days=None))
        self.assertNotIn("保有:", text)

    def test_target_distance_shown(self):
        text = self._get_text(_make_holding(current_price=185.0, target_price=210.0))
        self.assertIn("目標まで:", text)
        self.assertIn("210.00", text)

    def test_target_absent_when_none(self):
        text = self._get_text(_make_holding(target_price=None))
        self.assertNotIn("目標まで:", text)

    def test_reasons_shown_in_detail(self):
        text = self._get_text(_make_holding(), detail=True)
        self.assertIn("理由:", text)
        self.assertIn("明確な売りシグナルなし", text)

    def test_reasons_hidden_without_detail(self):
        text = self._get_text(_make_holding(), detail=False)
        self.assertNotIn("理由:", text)


if __name__ == "__main__":
    unittest.main()
