"""
scoring モジュールのテスト

テスト対象:
- compute_score(): 各サブスコア計算・final_score・ランク・理由
- 市場環境 multiplier の効果
- 境界値・データ欠損時の挙動
"""

import unittest
from unittest.mock import patch

_CONFIG_MOCK = {
    "signal": {
        "pattern_a_rsi_min": "50",
        "pattern_a_rsi_max": "70",
        "pattern_a_volume_ratio_min": "1.2",
        "pattern_a_52w_high_pct": "0.92",
        "pattern_b_rsi_min": "40",
        "pattern_b_rsi_max": "60",
        "pattern_b_ma25_touch_pct": "0.98",
        "pattern_b_lookback_days": "5",
        "max_candidates_per_pattern": "3",
    },
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
    "logging": {"log_dir": "/tmp", "log_level": "INFO"},
}

with patch("src.config.CONFIG", _CONFIG_MOCK):
    from src.scoring import (
        _calc_trend_score,
        _calc_pattern_a_score,
        _calc_pattern_b_score,
        _calc_liquidity_score,
        _recommend_rank,
        compute_score,
    )


def _base_ticker(**overrides) -> dict:
    """テスト用の銘柄辞書を生成する。"""
    tk = {
        "ticker": "7203", "market": "JP", "name": "テスト株",
        "close": 3000.0, "adj_close": 3000.0, "open": 2950.0,
        "ma5": 2950.0, "ma25": 2900.0, "ma75": 2800.0, "ma200": 2600.0,
        "ma5_5d_ago": 2900.0, "ma25_5d_ago": 2870.0, "ma200_5d_ago": 2560.0,
        "volume": 1500000, "vol_20d_avg": 1000000,
        "high_52w": 3100.0, "low_52w": 2000.0,
        "rsi14": 60.0, "atr14": 80.0,
        "est_turnover": 4_500_000_000, "est_market_cap": 50_000_000_000,
    }
    tk.update(overrides)
    return tk


class TrendScoreTests(unittest.TestCase):

    def test_perfect_alignment_and_strong_growth(self):
        """完全MA整列 + 強い上昇 → 高スコア"""
        tk = _base_ticker(
            ma5=3000, ma25=2900, ma75=2800, ma200=2600,
            ma200_5d_ago=2560,  # growth = (2600-2560)/2560 ≈ 1.56%
            close=3000,
        )
        score = _calc_trend_score(tk)
        self.assertGreaterEqual(score, 80)

    def test_no_alignment(self):
        """MA逆転 → 低スコア"""
        tk = _base_ticker(
            ma5=2500, ma25=2600, ma75=2800, ma200=3000,
            ma200_5d_ago=3050, close=2500,
        )
        score = _calc_trend_score(tk)
        self.assertLessEqual(score, 10)

    def test_partial_alignment(self):
        """MA75 >= MA200 のみ → 中間スコア"""
        tk = _base_ticker(
            ma5=2700, ma25=2650, ma75=2800, ma200=2600,
            ma200_5d_ago=2595, close=2700,
        )
        score = _calc_trend_score(tk)
        self.assertGreater(score, 20)
        self.assertLess(score, 80)

    def test_missing_data_returns_zero(self):
        """主要 MA が全て None → 0 点"""
        tk = _base_ticker(
            ma5=None, ma25=None, ma75=None, ma200=None,
            ma200_5d_ago=None, close=None,
        )
        score = _calc_trend_score(tk)
        self.assertEqual(score, 0)


class PatternAScoreTests(unittest.TestCase):

    def test_strong_breakout(self):
        """52週高値到達 + 出来高急増 + RSI適正帯 → 高スコア"""
        tk = _base_ticker(
            close=3080, high_52w=3100,    # ratio = 0.9935
            volume=2500000, vol_20d_avg=1000000,  # ratio = 2.5
            rsi14=60,
        )
        score = _calc_pattern_a_score(tk)
        self.assertGreaterEqual(score, 85)

    def test_weak_breakout(self):
        """52週高値から遠い → 低スコア"""
        tk = _base_ticker(
            close=2500, high_52w=3100,    # ratio ≈ 0.806
            volume=800000, vol_20d_avg=1000000,  # ratio < 1.0
            rsi14=35,
        )
        score = _calc_pattern_a_score(tk)
        self.assertLessEqual(score, 10)

    def test_relaxed_52w_threshold(self):
        """0.92 でもスコアが付く（緩和方針の確認）"""
        tk = _base_ticker(
            close=2860, high_52w=3100,    # ratio ≈ 0.923
            volume=1000000, vol_20d_avg=1000000,
            rsi14=60,
        )
        score = _calc_pattern_a_score(tk)
        self.assertGreater(score, 30)

    def test_relaxed_volume_threshold(self):
        """出来高 1.2 倍でもスコアが付く（緩和方針の確認）"""
        tk = _base_ticker(
            close=3080, high_52w=3100,
            volume=1250000, vol_20d_avg=1000000,  # ratio = 1.25
            rsi14=60,
        )
        score = _calc_pattern_a_score(tk)
        self.assertGreater(score, 50)


class PatternBScoreTests(unittest.TestCase):

    def test_good_pullback_bounce(self):
        """押し目タッチ + 反発（陽線）+ MA25上昇 → 高スコア"""
        tk = _base_ticker(
            close=2920, open=2880, ma25=2900, ma25_5d_ago=2870,
        )
        recent_low = 2840  # pullback_ratio = 2840/2900 ≈ 0.979
        score = _calc_pattern_b_score(tk, recent_low)
        self.assertGreaterEqual(score, 70)

    def test_no_pullback(self):
        """押し目なし → 低い押し目スコア"""
        tk = _base_ticker(
            close=3100, open=3050, ma25=2900, ma25_5d_ago=2870,
        )
        recent_low = 3000  # pullback_ratio = 3000/2900 ≈ 1.034
        score = _calc_pattern_b_score(tk, recent_low)
        self.assertLessEqual(score, 60)

    def test_no_recent_low_data(self):
        """recent_low_5d が None → pullback_comp = 0"""
        tk = _base_ticker(close=2920, ma25=2900)
        score = _calc_pattern_b_score(tk, None)
        self.assertLessEqual(score, 60)


class LiquidityScoreTests(unittest.TestCase):

    def test_jp_high_turnover(self):
        """30億円以上 → 100"""
        tk = _base_ticker(est_turnover=5_000_000_000, market="JP")
        self.assertEqual(_calc_liquidity_score(tk), 100)

    def test_jp_medium_turnover(self):
        """10億〜30億 → 80"""
        tk = _base_ticker(est_turnover=1_500_000_000, market="JP")
        self.assertEqual(_calc_liquidity_score(tk), 80)

    def test_jp_low_turnover(self):
        """3億〜5億 → 30"""
        tk = _base_ticker(est_turnover=350_000_000, market="JP")
        self.assertEqual(_calc_liquidity_score(tk), 30)

    def test_us_high_turnover(self):
        """$200M以上 → 100"""
        tk = _base_ticker(est_turnover=300_000_000, market="US")
        self.assertEqual(_calc_liquidity_score(tk), 100)


class RecommendRankTests(unittest.TestCase):

    def test_s_rank(self):
        self.assertEqual(_recommend_rank(90), "S")

    def test_a_rank(self):
        self.assertEqual(_recommend_rank(78), "A")

    def test_b_rank(self):
        self.assertEqual(_recommend_rank(65), "B")

    def test_c_rank(self):
        self.assertEqual(_recommend_rank(55), "C")

    def test_watch_rank(self):
        self.assertEqual(_recommend_rank(30), "Watch")

    def test_boundary_85(self):
        self.assertEqual(_recommend_rank(85), "S")

    def test_boundary_50(self):
        self.assertEqual(_recommend_rank(50), "C")


class ComputeScoreTests(unittest.TestCase):

    def test_normal_mode_no_reduction(self):
        """normal モード → multiplier = 1.0"""
        tk = _base_ticker()
        result = compute_score(tk, "normal", recent_low_5d=2840)
        self.assertEqual(result["market_multiplier"], 1.0)
        self.assertIn("recommend_rank", result)
        self.assertIn("recommend_reason", result)

    def test_caution_mode_reduces_score(self):
        """caution モード → final_score が normal より低い"""
        tk = _base_ticker()
        normal  = compute_score(tk, "normal",  recent_low_5d=2840)
        caution = compute_score(tk, "caution", recent_low_5d=2840)
        self.assertLessEqual(caution["final_score"], normal["final_score"])
        self.assertEqual(caution["market_multiplier"], 0.88)

    def test_full_caution_mode_reduces_more(self):
        """full_caution → さらに大きい減点"""
        tk = _base_ticker()
        caution      = compute_score(tk, "caution",      recent_low_5d=2840)
        full_caution = compute_score(tk, "full_caution", recent_low_5d=2840)
        self.assertLessEqual(full_caution["final_score"], caution["final_score"])
        self.assertEqual(full_caution["market_multiplier"], 0.75)

    def test_final_score_clamped_0_100(self):
        """final_score は 0〜100 の範囲に収まる"""
        tk = _base_ticker(
            ma200=None, ma200_5d_ago=None,
            high_52w=None, volume=0, rsi14=None,
            est_turnover=0,
        )
        result = compute_score(tk, "normal")
        self.assertGreaterEqual(result["final_score"], 0)
        self.assertLessEqual(result["final_score"], 100)

    def test_all_scores_present(self):
        """返り値に必要な全キーが含まれる"""
        tk = _base_ticker()
        result = compute_score(tk, "normal")
        expected_keys = {
            "trend_score", "pattern_a_score", "pattern_b_score",
            "liquidity_score", "base_score", "market_multiplier",
            "final_score", "recommend_rank", "recommend_reason",
        }
        self.assertTrue(expected_keys.issubset(result.keys()))

    def test_reason_mentions_caution(self):
        """caution モードの理由文に警戒モードが含まれる"""
        tk = _base_ticker()
        result = compute_score(tk, "caution", recent_low_5d=2840)
        self.assertIn("警戒モード", result["recommend_reason"])


if __name__ == "__main__":
    unittest.main()
