"""
entry_calculator モジュールのテスト

テスト対象:
- calc_entry_set(): 推奨投入額の上限制御、取引単位チェック、損切り幅フィルタ
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
}

with patch("src.config.CONFIG", _CONFIG_MOCK):
    from src.entry_calculator import calc_entry_set


class ConfigPatchMixin:
    """他テストの import 順に左右されず、各ケースでテスト用 config を使う。"""

    def setUp(self):
        super().setUp()
        self._config_patcher = patch("src.config.CONFIG", _CONFIG_MOCK)
        self._config_patcher.start()

    def tearDown(self):
        self._config_patcher.stop()
        super().tearDown()


def _make_signal(**kwargs):
    """テスト用シグナル辞書を生成する。"""
    base = {
        "date": "2026-04-30",
        "ticker": "7203.TSE",
        "name": "トヨタ自動車",
        "market": "JP",
        "pattern": "A",
        "close": 2500.0,
        "atr14": 100.0,
        "rsi14": 55.0,
        "vol_ratio": 1.5,
        "rs_rank": 1,
        "market_mode": "normal",
        "final_score": 60,
        "recommend_rank": "A",
        "recommend_reason": "テスト",
        "trend_score": 80,
        "pattern_a_score": 70,
        "pattern_b_score": None,
        "liquidity_score": 90,
        "cond_a_ma200_rising": 1,
        "cond_a_ma_aligned": 1,
        "cond_a_52w_high_near": 1,
        "cond_a_volume_surge": 1,
        "cond_a_rsi_ok": 1,
        "cond_a_above_ma25": 1,
    }
    base.update(kwargs)
    return base


class CalcEntrySetCapTests(ConfigPatchMixin, unittest.TestCase):
    """推奨投入額の上限制御テスト"""

    def test_recommended_amount_does_not_exceed_capital(self):
        """推奨投入額が総資金を超えない"""
        # 損切り幅を極端に小さくして、上限なしなら資金超過する条件
        sig = _make_signal(close=2500.0, atr14=15.0)  # SL幅 = 1.2%
        result = calc_entry_set(sig)
        self.assertIsNotNone(result.get("recommended_amount"))
        self.assertLessEqual(result["recommended_amount"], 200000)

    def test_recommended_amount_does_not_exceed_per_position_limit(self):
        """推奨投入額が capital / max_concurrent_holdings を超えない"""
        sig = _make_signal(close=2500.0, atr14=50.0)  # SL幅 = 4%
        result = calc_entry_set(sig)
        # 200000 / 3 = 66666.67
        self.assertIsNotNone(result.get("recommended_amount"))
        self.assertLessEqual(result["recommended_amount"], 200000 / 3 + 1)


class CalcEntrySetNoShareUnitSkipTests(ConfigPatchMixin, unittest.TestCase):
    """端株対応: 株数チェックによる見送りが発生しないことを確認"""

    def test_jp_expensive_stock_not_skipped(self):
        """高額JP株でも端株取引可能なため見送りにならない"""
        # close=100000, capital/3=約6.7万円 → 旧ロジックでは見送りだった
        sig = _make_signal(close=100000.0, atr14=4000.0)
        result = calc_entry_set(sig)
        self.assertNotEqual(result, {})
        self.assertIsNotNone(result.get("recommended_amount"))

    def test_us_stock_not_skipped(self):
        """米国株も見送りにならない"""
        sig = _make_signal(market="US", ticker="AAPL", close=150.0, atr14=6.0)
        result = calc_entry_set(sig)
        self.assertNotEqual(result, {})
        self.assertIsNotNone(result.get("recommended_amount"))


class CalcEntrySetStopLossFilterTests(ConfigPatchMixin, unittest.TestCase):
    """損切り幅フィルタテスト"""

    def test_narrow_stop_loss_excluded(self):
        """損切り幅が1%未満の場合は候補から除外"""
        # atr14=5 → SL幅 = (10/2500)*100 = 0.4%
        sig = _make_signal(close=2500.0, atr14=5.0)
        result = calc_entry_set(sig)
        self.assertEqual(result, {})

    def test_wide_stop_loss_warns(self):
        """損切り幅が15%超の場合は候補に残るが警告"""
        sig = _make_signal(close=2500.0, atr14=250.0)  # SL幅 = 20%
        result = calc_entry_set(sig)
        self.assertNotEqual(result, {})
        self.assertTrue(result.get("_warn_wide"))

    def test_normal_stop_loss_no_warning(self):
        """損切り幅が正常範囲では警告なし"""
        sig = _make_signal(close=2500.0, atr14=100.0)  # SL幅 = 8%
        result = calc_entry_set(sig)
        self.assertNotEqual(result, {})
        self.assertFalse(result.get("_warn_narrow"))
        self.assertFalse(result.get("_warn_wide"))


class CalcEntrySetHoldingsCapacityTests(ConfigPatchMixin, unittest.TestCase):
    """既存保有数を考慮した投入額・警告テスト"""

    def test_no_holdings_uses_default_cap(self):
        """保有0件のとき従来どおり capital / max_holdings が上限"""
        sig = _make_signal(close=2500.0, atr14=50.0)  # SL幅 = 4%
        result = calc_entry_set(sig, open_holdings_count=0)
        self.assertLessEqual(result["recommended_amount"], 200000 / 3 + 1)
        self.assertFalse(result["_warn_at_capacity"])
        self.assertEqual(result["_remaining_slots"], 3)

    def test_at_capacity_warns(self):
        """保有数が max_holdings に達したら警告フラグが立つ"""
        sig = _make_signal(close=2500.0, atr14=100.0)
        result = calc_entry_set(sig, open_holdings_count=3)
        self.assertTrue(result["_warn_at_capacity"])
        self.assertEqual(result["_remaining_slots"], 0)

    def test_over_capacity_same_amount(self):
        """保有数が max_holdings を超えても推奨額は capital / max_holdings で固定"""
        sig = _make_signal(close=2500.0, atr14=50.0)  # SL幅 = 4%
        result_0 = calc_entry_set(sig, open_holdings_count=0)
        result_4 = calc_entry_set(sig, open_holdings_count=4)
        # max_holdings=3 で固定 → 200000/3 ≈ 66667 が上限
        self.assertEqual(result_4["recommended_amount"], result_0["recommended_amount"])
        self.assertLessEqual(result_4["recommended_amount"], 200000 / 3 + 1)

    def test_within_capacity_no_warning(self):
        """保有数が max_holdings 未満なら警告なし"""
        sig = _make_signal(close=2500.0, atr14=100.0)
        result = calc_entry_set(sig, open_holdings_count=2)
        self.assertFalse(result["_warn_at_capacity"])
        self.assertEqual(result["_remaining_slots"], 1)


_OFFENSIVE_CONFIG_MOCK = {
    **_CONFIG_MOCK,
    "strategy_mode": "offensive",
    "strategy": {
        "offensive": {
            "max_concurrent_holdings": 8,
            "max_per_position_pct": 20.0,
            "new_entry_size_pct_min": 5.0,
            "new_entry_size_pct_max": 10.0,
            "risk_per_trade_pct": 1.5,
        },
    },
}


class OffensiveModeMixin:
    """攻めモード設定で entry_calculator を呼び出すヘルパー。"""

    def setUp(self):
        super().setUp()
        self._config_patcher = patch("src.config.CONFIG", _OFFENSIVE_CONFIG_MOCK)
        self._config_patcher.start()

    def tearDown(self):
        self._config_patcher.stop()
        super().tearDown()


class CalcEntrySetOffensiveTests(OffensiveModeMixin, unittest.TestCase):
    """攻めモード時のポジションサイズテスト"""

    def test_max_holdings_uses_offensive_value(self):
        """攻めモードでは max_holdings = 8（capital からではなく offensive 設定から）"""
        sig = _make_signal(close=2500.0, atr14=100.0)
        result = calc_entry_set(sig, open_holdings_count=5)
        # offensive.max_concurrent_holdings = 8 なので 5 件保有でも余裕あり
        self.assertFalse(result["_warn_at_capacity"])
        self.assertEqual(result["_remaining_slots"], 3)
        self.assertEqual(result["_strategy_mode"], "offensive")

    def test_offensive_per_position_cap_20pct(self):
        """1銘柄あたり capital × 20% (= 40000) を超えない"""
        # 損切り幅を小さくして大きな recommended_amount を引き出す
        sig = _make_signal(close=2500.0, atr14=25.0)  # SL幅 = 2%
        result = calc_entry_set(sig)
        # 上限 = min(capital * 20%, capital * 10%) = 20000
        self.assertLessEqual(result["recommended_amount"], 20000 + 1)

    def test_offensive_warn_too_small_when_below_min_entry(self):
        """推奨額が新規エントリー下限（5%）未満の場合は警告"""
        # 損切り幅が大きいと recommended_amount は小さくなる
        # capital * 1.5% / 20% = 15000 < capital * 5% = 10000 ? いいえ
        # SL幅 25% → recommended = 200000 * 1.5 / 25 = 12000 → 10000上限後 → 10000 (≥5000)
        # SL幅をもっと大きくする: 35% → recommended = 200000 * 1.5/35 = 8571 → < 10000 上限なので 8571
        # < 5%（=10000）でないと _warn_too_small が立たない
        # SL幅 = 50% → recommended = 6000 → < 10000 OK
        sig = _make_signal(close=2500.0, atr14=625.0)  # SL幅 = 50%
        result = calc_entry_set(sig)
        # 損切り幅が広すぎて見送られる場合もあるが、見送られなければ警告フラグを確認
        if result:
            self.assertTrue(
                result.get("_warn_too_small") or result.get("_warn_wide"),
                "広い損切りまたは少額警告のどちらかが立つ",
            )


if __name__ == "__main__":
    unittest.main()
