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


class CalcEntrySetCapTests(unittest.TestCase):
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


class CalcEntrySetJPUnitTests(unittest.TestCase):
    """日本株の100株単位チェックテスト"""

    def test_jp_stock_100_share_unit(self):
        """日本株は100株単位で丸められる"""
        sig = _make_signal(close=500.0, atr14=20.0)  # SL幅 = 8%
        result = calc_entry_set(sig)
        self.assertIsNotNone(result.get("recommended_amount"))
        # recommended_amount / close が100の倍数になる
        shares = result["recommended_amount"] / 500.0
        self.assertEqual(shares % 100, 0)

    def test_jp_stock_skip_when_unit_too_expensive(self):
        """100株の購入に必要な金額が投入上限を超える場合は見送り"""
        # close=1000 → 100株=10万円、capital/3=約6.7万円 → 購入不可
        sig = _make_signal(close=1000.0, atr14=40.0)
        result = calc_entry_set(sig)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("_skip_reason"), "資金不足のため見送り")

    def test_us_stock_no_100_share_rounding(self):
        """米国株は100株単位の制約がない"""
        sig = _make_signal(market="US", ticker="AAPL", close=150.0, atr14=6.0)
        result = calc_entry_set(sig)
        self.assertIsNotNone(result.get("recommended_amount"))
        self.assertIsNone(result.get("_skip_reason"))


class CalcEntrySetStopLossFilterTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
