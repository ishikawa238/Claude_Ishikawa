"""
risk モジュールのテスト

テスト対象:
- calc_risk_metrics(): VaR, Beta, Sharpe Ratio の計算正確性
- エッジケース: データ不足、分散ゼロ、ベンチマーク不一致
"""

import unittest
from unittest.mock import patch

import numpy as np


_CONFIG_MOCK = {
    "risk": {
        "risk_free_rate_jp": "0.001",
        "risk_free_rate_us": "0.05",
    },
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
    "slack": {"webhook_url": "https://XXXXX"},
}

with patch("src.config.CONFIG", _CONFIG_MOCK):
    from src.risk import calc_risk_metrics


class CalcRiskMetricsBasicTests(unittest.TestCase):
    """基本的な計算結果の検証"""

    def test_var_99_negative(self):
        """VaR 99% は負の値（損失方向）を返す"""
        # 下落含む価格列
        prices = [100, 102, 99, 101, 97, 103, 98, 100, 96, 104, 99, 101]
        bm = list(prices)  # ベンチマーク同一
        result = calc_risk_metrics(prices, bm, market="JP")
        self.assertIsNotNone(result["var_99_1d"])
        self.assertLess(result["var_99_1d"], 0)

    def test_beta_near_one_for_identical_series(self):
        """ベンチマークと同一の価格列なら Beta ≈ 1.0"""
        prices = [100 + i * 0.5 + (i % 3 - 1) * 0.2 for i in range(60)]
        result = calc_risk_metrics(prices, list(prices), market="JP")
        self.assertIsNotNone(result["beta_60d"])
        self.assertAlmostEqual(result["beta_60d"], 1.0, places=1)

    def test_sharpe_positive_for_uptrend(self):
        """上昇トレンドの価格列なら Sharpe > 0"""
        prices = [100 + i * 0.3 for i in range(60)]
        bm = [100 + i * 0.2 for i in range(60)]
        result = calc_risk_metrics(prices, bm, market="JP")
        self.assertIsNotNone(result["sharpe_60d"])
        self.assertGreater(result["sharpe_60d"], 0)

    def test_sharpe_negative_for_downtrend(self):
        """下降トレンドの価格列なら Sharpe < 0"""
        prices = [100 - i * 0.5 for i in range(60)]
        bm = [100 + i * 0.1 for i in range(60)]
        result = calc_risk_metrics(prices, bm, market="JP")
        self.assertIsNotNone(result["sharpe_60d"])
        self.assertLess(result["sharpe_60d"], 0)


class CalcRiskMetricsEdgeCaseTests(unittest.TestCase):
    """エッジケースの検証"""

    def test_insufficient_data_returns_none(self):
        """データ10件未満では全て None"""
        prices = [100, 101, 102]
        bm = [100, 101, 102]
        result = calc_risk_metrics(prices, bm)
        self.assertIsNone(result["var_99_1d"])
        self.assertIsNone(result["beta_60d"])
        self.assertIsNone(result["sharpe_60d"])

    def test_minimum_data_returns_var(self):
        """データ10件丁度で VaR は計算される"""
        prices = [100 + i for i in range(10)]
        bm = [100 + i for i in range(10)]
        result = calc_risk_metrics(prices, bm)
        self.assertIsNotNone(result["var_99_1d"])

    def test_constant_prices_zero_variance(self):
        """全同値の価格列では分散ゼロで Sharpe は None"""
        prices = [100.0] * 20
        bm = [100.0] * 20
        result = calc_risk_metrics(prices, bm)
        # リターンが全て0 → std = 0 → Sharpe 計算不可
        self.assertIsNone(result["sharpe_60d"])

    def test_benchmark_shorter_skips_beta(self):
        """ベンチマーク日数が不足している場合は Beta が None"""
        prices = [100 + i * 0.3 for i in range(30)]
        bm = [100 + i * 0.3 for i in range(10)]  # 短すぎる
        result = calc_risk_metrics(prices, bm)
        self.assertIsNone(result["beta_60d"])
        # VaR はベンチマーク不要なので計算される
        self.assertIsNotNone(result["var_99_1d"])

    def test_us_market_uses_higher_risk_free_rate(self):
        """US市場では高い無リスク金利が使われ、Sharpe が下がる"""
        prices = [100 + i * 0.3 for i in range(60)]
        bm = [100 + i * 0.2 for i in range(60)]
        result_jp = calc_risk_metrics(prices, bm, market="JP")
        result_us = calc_risk_metrics(prices, bm, market="US")
        # US は risk_free_rate が高い → excess return が小さい → Sharpe が低い
        self.assertIsNotNone(result_jp["sharpe_60d"])
        self.assertIsNotNone(result_us["sharpe_60d"])
        self.assertGreater(result_jp["sharpe_60d"], result_us["sharpe_60d"])

    def test_result_keys_always_present(self):
        """返り値に var_99_1d, beta_60d, sharpe_60d のキーが必ず含まれる"""
        result = calc_risk_metrics([100], [100])
        self.assertIn("var_99_1d", result)
        self.assertIn("beta_60d", result)
        self.assertIn("sharpe_60d", result)


if __name__ == "__main__":
    unittest.main()
