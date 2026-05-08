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
        # サブスコア
        "_trend_score": 82,
        "_pattern_a_score": 75,
        "_pattern_b_score": 40,
        "_liquidity_score": 100,
        # テクニカル指標
        "_high_52w": 155.0,
        "_close": 150.0,
        "_ma5": 148.0,
        "_ma25": 145.0,
        "_ma75": 140.0,
        "_ma200": 130.0,
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
        # テクニカル指標
        "tech_ma5": 183.0,
        "tech_ma25": 180.0,
        "tech_ma200": 160.0,
        "tech_rsi14": 55.0,
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

    # --- サブスコア内訳テスト ---
    def test_sub_scores_shown(self):
        text = self._get_text(_make_entry_set(
            _trend_score=82, _pattern_a_score=75,
            _pattern_b_score=40, _liquidity_score=100,
        ))
        self.assertIn("トレンド:82", text)
        self.assertIn("ブレイクアウト:75", text)
        self.assertIn("押し目:40", text)
        self.assertIn("流動性:100", text)

    def test_sub_scores_absent_when_none(self):
        text = self._get_text(_make_entry_set(
            _trend_score=None, _pattern_a_score=None,
            _pattern_b_score=None, _liquidity_score=None,
        ))
        # サブスコア行が出力されないことを確認
        self.assertNotIn("トレンド:", text)
        self.assertNotIn("流動性:", text)

    # --- 52週高値テスト ---
    def test_52w_high_distance_shown(self):
        text = self._get_text(_make_entry_set(_close=150.0, _high_52w=155.0))
        self.assertIn("52w高値まで:", text)
        self.assertIn("3.3%", text)

    def test_52w_high_at_new_high(self):
        text = self._get_text(_make_entry_set(_close=160.0, _high_52w=155.0))
        self.assertIn("52w高値: 更新中", text)

    def test_52w_high_absent_when_none(self):
        text = self._get_text(_make_entry_set(_close=None, _high_52w=None))
        self.assertNotIn("52w高値", text)

    # --- MA整列テスト ---
    def test_ma_alignment_full(self):
        text = self._get_text(_make_entry_set(
            _ma5=148.0, _ma25=145.0, _ma75=140.0, _ma200=130.0,
        ))
        self.assertIn("MA:", text)
        self.assertIn("完全整列", text)
        self.assertIn("5↑", text)

    def test_ma_alignment_partial(self):
        text = self._get_text(_make_entry_set(
            _ma5=140.0, _ma25=145.0, _ma75=140.0, _ma200=130.0,
        ))
        self.assertIn("部分整列", text)
        self.assertIn("5↓", text)

    def test_ma_alignment_absent_when_none(self):
        text = self._get_text(_make_entry_set(
            _ma5=None, _ma25=None, _ma75=None, _ma200=None,
        ))
        self.assertNotIn("MA:", text)


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

    # --- テクニカルスナップショットテスト ---
    def test_tech_ma25_shown_in_detail(self):
        text = self._get_text(_make_holding(
            tech_ma25=180.0, tech_rsi14=55.0, current_price=185.0,
        ), detail=True)
        self.assertIn("MA25: 180.00", text)
        self.assertIn("上方", text)
        self.assertIn("RSI: 55", text)

    def test_tech_trend_up_shown_in_detail(self):
        text = self._get_text(_make_holding(
            tech_ma5=183.0, tech_ma25=180.0,
        ), detail=True)
        self.assertIn("MA5↑MA25", text)
        self.assertIn("上昇中", text)

    def test_tech_trend_down_shown_in_detail(self):
        text = self._get_text(_make_holding(
            tech_ma5=178.0, tech_ma25=180.0,
        ), detail=True)
        self.assertIn("MA5↓MA25", text)
        self.assertIn("下降中", text)

    def test_tech_hidden_without_detail(self):
        text = self._get_text(_make_holding(
            tech_ma25=180.0, tech_rsi14=55.0,
        ), detail=False)
        self.assertNotIn("MA25:", text)
        self.assertNotIn("RSI:", text)

    def test_tech_absent_when_none(self):
        text = self._get_text(_make_holding(
            tech_ma5=None, tech_ma25=None, tech_rsi14=None,
        ), detail=True)
        self.assertNotIn("MA25:", text)
        self.assertNotIn("MA5", text)


class PortfolioSummaryTests(unittest.TestCase):

    def test_jpy_only(self):
        holdings = [
            _make_holding(market="JP", pnl_amount=5000.0, holding_days=10),
            _make_holding(market="JP", pnl_amount=7500.0, holding_days=30),
        ]
        result = notifier._portfolio_summary(holdings)
        self.assertIn("+12,500円", result)
        self.assertIn("平均保有日数: 20日", result)

    def test_usd_only(self):
        holdings = [
            _make_holding(market="US", pnl_amount=100.0, holding_days=5),
            _make_holding(market="US", pnl_amount=-50.0, holding_days=15),
        ]
        result = notifier._portfolio_summary(holdings)
        self.assertIn("+50USD", result)
        self.assertIn("平均保有日数: 10日", result)

    def test_mixed_markets(self):
        holdings = [
            _make_holding(market="JP", pnl_amount=5000.0, holding_days=10),
            _make_holding(market="US", pnl_amount=200.0, holding_days=20),
        ]
        result = notifier._portfolio_summary(holdings)
        self.assertIn("+5,000円", result)
        self.assertIn("+200USD", result)

    def test_empty_holdings(self):
        result = notifier._portfolio_summary([])
        self.assertEqual(result, "")

    def test_no_pnl_amount(self):
        holdings = [
            _make_holding(pnl_amount=None, holding_days=10),
        ]
        result = notifier._portfolio_summary(holdings)
        self.assertIn("算出不可", result)
        self.assertIn("平均保有日数: 10日", result)


if __name__ == "__main__":
    unittest.main()
