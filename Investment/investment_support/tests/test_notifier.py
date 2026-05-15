"""
notifier.py のブロック生成テスト

_entry_set_block() と _holding_block() の出力文字列に
期待するフィールドが含まれるか検証する。
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent  # Ishikawa238/
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

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

import src.config as _src_config
_src_config.CONFIG = _CONFIG_MOCK
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
        self.assertIn("損切:", text)
        self.assertIn("→ 利確:", text)
        self.assertIn("RR2:1", text)

    def test_stop_pct_has_minus_sign(self):
        text = self._get_text(_make_entry_set(stop_loss_pct=8.0))
        self.assertIn("-8.0%", text)

    def test_warn_narrow_shown(self):
        text = self._get_text(_make_entry_set(_warn_narrow=True, _warn_wide=False))
        self.assertIn("損切り幅が狭い", text)

    def test_warn_wide_shown(self):
        text = self._get_text(_make_entry_set(_warn_narrow=False, _warn_wide=True))
        self.assertIn("損切り幅が広め", text)

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
        self.assertIn("52w高値:", text)
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
        self.assertIn("保有: 15日", text)

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
    def test_tech_rsi_and_trend_shown_in_detail(self):
        text = self._get_text(_make_holding(
            tech_ma5=183.0, tech_ma25=180.0, tech_rsi14=55.0, current_price=185.0,
        ), detail=True)
        self.assertIn("MA5↑MA25", text)
        self.assertIn("RSI:55", text)

    def test_tech_trend_up_shown_in_detail(self):
        text = self._get_text(_make_holding(
            tech_ma5=183.0, tech_ma25=180.0,
        ), detail=True)
        self.assertIn("MA5↑MA25", text)

    def test_tech_trend_down_shown_in_detail(self):
        text = self._get_text(_make_holding(
            tech_ma5=178.0, tech_ma25=180.0,
        ), detail=True)
        self.assertIn("MA5↓MA25", text)

    def test_tech_hidden_without_detail(self):
        text = self._get_text(_make_holding(
            tech_ma5=183.0, tech_ma25=180.0, tech_rsi14=55.0,
        ), detail=False)
        self.assertNotIn("MA5", text)
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


def _make_near_miss(**kwargs):
    base = {
        "ticker": "6758",
        "name": "ソニーグループ",
        "market": "JP",
        "pattern": "A",
        "label": "watch",
        "final_score": 43,
        "recommend_rank": "Watch",
        "recommend_reason": "条件の充足が限定的",
        "trend_score": 72,
        "pattern_a_score": 35,
        "pattern_b_score": 50,
        "liquidity_score": 80,
        "cond_a_ma200_rising": 1,
        "cond_a_ma_aligned": 1,
        "cond_a_52w_high_near": 0,
        "cond_a_volume_surge": 1,
        "cond_a_rsi_ok": 0,
        "cond_a_above_ma25": 1,
        "cond_b_ma200_rising": None,
        "cond_b_ma_aligned": None,
        "cond_b_pullback_touched": None,
        "cond_b_bounced": None,
        "cond_b_ma25_rising": None,
        "cond_b_rsi_ok": None,
    }
    base.update(kwargs)
    return base


class NearMissSectionTests(unittest.TestCase):

    def _get_texts(self, near_misses):
        blocks = notifier._near_miss_section(near_misses)
        return [b["text"]["text"] for b in blocks if b.get("type") == "section"]

    def test_empty_returns_no_blocks(self):
        self.assertEqual(notifier._near_miss_section(None), [])
        self.assertEqual(notifier._near_miss_section([]), [])

    def test_header_shown(self):
        texts = self._get_texts([_make_near_miss()])
        self.assertTrue(any("惜しかった銘柄" in t for t in texts))

    def test_score_gap_shown_for_score_short(self):
        texts = self._get_texts([_make_near_miss(final_score=43, near_miss_type="score_short")])
        combined = "\n".join(texts)
        self.assertIn("43点", combined)
        self.assertIn("あと7点", combined)

    def test_filtered_type_shows_reason(self):
        texts = self._get_texts([_make_near_miss(
            final_score=72, near_miss_type="filtered", label="candidate",
        )])
        combined = "\n".join(texts)
        self.assertIn("72点", combined)
        self.assertIn("損切り幅・資金条件で見送り", combined)
        self.assertNotIn("あと", combined)

    def test_demoted_type_shows_reason(self):
        texts = self._get_texts([_make_near_miss(
            final_score=65, near_miss_type="demoted",
        )])
        combined = "\n".join(texts)
        self.assertIn("65点", combined)
        self.assertIn("警戒モード", combined)

    def test_sub_scores_shown(self):
        texts = self._get_texts([_make_near_miss(
            trend_score=72, pattern_a_score=35,
            pattern_b_score=50, liquidity_score=80,
        )])
        combined = "\n".join(texts)
        self.assertIn("トレンド:72", combined)
        self.assertIn("ブレイクアウト:35", combined)

    def test_conditions_pass_fail_shown(self):
        texts = self._get_texts([_make_near_miss(
            cond_a_ma200_rising=1, cond_a_52w_high_near=0,
        )])
        combined = "\n".join(texts)
        self.assertIn("✅MA200↑", combined)
        self.assertIn("❌52w高値", combined)

    def test_pattern_b_conditions_shown(self):
        nm = _make_near_miss(
            pattern="B",
            cond_a_ma200_rising=None, cond_a_ma_aligned=None,
            cond_a_52w_high_near=None, cond_a_volume_surge=None,
            cond_a_rsi_ok=None, cond_a_above_ma25=None,
            cond_b_ma200_rising=1, cond_b_ma_aligned=1,
            cond_b_pullback_touched=0, cond_b_bounced=0,
            cond_b_ma25_rising=1, cond_b_rsi_ok=1,
        )
        texts = self._get_texts([nm])
        combined = "\n".join(texts)
        self.assertIn("✅MA200↑", combined)
        self.assertIn("❌MA25タッチ", combined)

    def test_reason_shown(self):
        texts = self._get_texts([_make_near_miss(recommend_reason="トレンドは良好")])
        combined = "\n".join(texts)
        self.assertIn("トレンドは良好", combined)

    def test_max_two_shown(self):
        nms = [_make_near_miss(ticker=f"T{i}") for i in range(5)]
        texts = self._get_texts(nms)
        combined = "\n".join(texts)
        self.assertIn("T0", combined)
        self.assertIn("T1", combined)
        self.assertNotIn("T2", combined)


class NoCandidatesReasonTests(unittest.TestCase):

    def test_no_stats_no_near_misses(self):
        result = notifier._no_candidates_reason(None, None, "normal")
        self.assertEqual(result, "")

    def test_no_candidates_scored_high_enough(self):
        stats = {"screened": 1200, "candidates": 0, "entry_sets": 0}
        result = notifier._no_candidates_reason(stats, None, "normal")
        self.assertIn("1,200銘柄", result)
        self.assertIn("candidate: 0銘柄", result)
        self.assertIn("スコア50点以上の銘柄がありません", result)

    def test_candidates_filtered_by_entry_calc(self):
        stats = {"screened": 1200, "candidates": 3, "entry_sets": 0}
        result = notifier._no_candidates_reason(stats, None, "normal")
        self.assertIn("3銘柄がcandidate判定されました", result)
        self.assertIn("損切り幅・資金条件で全て見送り", result)

    def test_caution_mode_shows_mode_reason(self):
        result = notifier._no_candidates_reason(None, None, "caution")
        self.assertIn("警戒モード", result)

    def test_top_score_from_near_misses(self):
        nms = [_make_near_miss(ticker="6758", name="ソニー", final_score=47)]
        result = notifier._no_candidates_reason(None, nms, "normal")
        self.assertIn("47点", result)
        self.assertIn("6758", result)


class ActionGuidanceTests(unittest.TestCase):
    """A-2: 損切り到達時のアクション導線テスト"""

    def test_forced_stop_has_action_guidance(self):
        """forced_stop の朝通知ブロックに成行売りの指示が含まれる"""
        from datetime import date
        market_env = {"mode": "normal", "n225_close": 38000, "n225_ma25": 37500,
                      "sp500_close": 5200, "sp500_ma25": 5100}
        holdings = [_make_holding(label="forced_stop", pnl_pct=-10.0)]
        # send_morning_report の blocks 生成を間接的にテスト
        # _build_tomorrow_notes で確認
        notes = notifier._build_tomorrow_notes(holdings)
        self.assertTrue(any("成行売り" in n for n in notes))

    def test_sl_near_has_guidance(self):
        """SL接近銘柄に「割込み時は即手仕舞い」の指示がある"""
        holdings = [_make_holding(label="sl_near", pnl_pct=-5.0)]
        notes = notifier._build_tomorrow_notes(holdings)
        self.assertTrue(any("即手仕舞い" in n for n in notes))


class CapacityWarningTests(unittest.TestCase):
    """A-2: 保有枠満杯の警告テスト"""

    def _get_text(self, es):
        block = notifier._entry_set_block(es)
        return block["text"]["text"]

    def test_at_capacity_shows_warning(self):
        """保有枠満杯で警告が表示される"""
        text = self._get_text(_make_entry_set(_warn_at_capacity=True, _remaining_slots=0))
        self.assertIn("保有枠満杯", text)

    def test_remaining_slots_shown(self):
        """保有枠に残りがある場合は残り枠数が表示される"""
        text = self._get_text(_make_entry_set(_warn_at_capacity=True, _remaining_slots=1))
        self.assertIn("残り1枠", text)

    def test_no_capacity_warning_when_not_at_capacity(self):
        """保有枠に余裕がある場合は警告なし"""
        text = self._get_text(_make_entry_set(_warn_at_capacity=False, _remaining_slots=2))
        self.assertNotIn("保有枠", text)


class DataInsufficientReasonTests(unittest.TestCase):
    """A-5: データ不足の通知表示テスト"""

    def test_data_insufficient_shown(self):
        """データ不足の銘柄数が表示される"""
        stats = {"screened": 100, "candidates": 0, "entry_sets": 0, "data_insufficient": 15}
        result = notifier._no_candidates_reason(stats, None, "normal")
        self.assertIn("データ不足", result)
        self.assertIn("15銘柄", result)

    def test_no_data_insufficient_when_zero(self):
        """データ不足が0件のとき表示されない"""
        stats = {"screened": 100, "candidates": 0, "entry_sets": 0, "data_insufficient": 0}
        result = notifier._no_candidates_reason(stats, None, "normal")
        self.assertNotIn("データ不足", result)


if __name__ == "__main__":
    unittest.main()
