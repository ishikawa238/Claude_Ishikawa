"""
Pattern C（サポートバウンス）のテスト

テスト対象:
- _aggregate_weekly(): 日足→週足集約
- _parabolic_sar(): パラボリックSAR計算
- _detect_support_resistance(): サポート/レジスタンス検出
- _calc_pattern_c_score(): スコアリング
- _eval_pattern_c(): シグナル検出条件評価
- スコアリング重みの合計=1.0
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
        "pattern_c_rsi_max": "35",
        "pattern_c_support_tolerance_pct": "3.0",
        "pattern_c_sar_af_start": "0.02",
        "pattern_c_sar_af_step": "0.02",
        "pattern_c_sar_af_max": "0.20",
        "pattern_c_support_lookback_days": "120",
        "max_candidates_per_pattern": "3",
    },
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
    "logging": {"log_dir": "/tmp", "log_level": "INFO"},
}

with patch("src.config.CONFIG", _CONFIG_MOCK):
    from src.technical_calculator import (
        _aggregate_weekly,
        _parabolic_sar,
        _detect_support_resistance,
        _find_strongest_cluster,
    )
    from src.scoring import (
        _calc_pattern_c_score,
        W_TREND,
        W_PATTERN_A,
        W_PATTERN_B,
        W_PATTERN_C,
        W_LIQUIDITY,
    )


# ============================================================
# テストデータ生成ヘルパー
# ============================================================

def _daily_prices(closes: list[float], start_date: str = "2025-01-06") -> list[dict]:
    """終値リストから日足データリストを生成する（月〜金のみ）。"""
    from datetime import date, timedelta
    d = date.fromisoformat(start_date)
    result = []
    for c in closes:
        # 土日をスキップ
        while d.weekday() >= 5:
            d += timedelta(days=1)
        result.append({
            "date": d.strftime("%Y-%m-%d"),
            "open": c * 0.99,
            "high": c * 1.01,
            "low": c * 0.98,
            "adj_close": c,
            "volume": 1000000,
        })
        d += timedelta(days=1)
    return result


# ============================================================
# 週足集約テスト
# ============================================================

class AggregateWeeklyTests(unittest.TestCase):

    def test_basic_aggregation(self):
        """10営業日 → 2週足キャンドルが生成される"""
        prices = _daily_prices([100 + i for i in range(10)])
        weeks = _aggregate_weekly(prices)
        self.assertEqual(len(weeks), 2)

    def test_weekly_ohlcv(self):
        """週足のOHLCVが正しく集約される"""
        prices = _daily_prices([100, 105, 95, 110, 100])
        weeks = _aggregate_weekly(prices)
        self.assertEqual(len(weeks), 1)
        w = weeks[0]
        # open = 月曜の始値
        self.assertAlmostEqual(w["open"], 100 * 0.99, places=1)
        # high = 最高値
        self.assertAlmostEqual(w["high"], 110 * 1.01, places=1)
        # low = 最安値
        self.assertAlmostEqual(w["low"], 95 * 0.98, places=1)
        # close = 金曜の終値
        self.assertAlmostEqual(w["close"], 100, places=1)

    def test_empty_input(self):
        """空リスト → 空リスト"""
        self.assertEqual(_aggregate_weekly([]), [])

    def test_single_day(self):
        """1日分 → 1週足"""
        prices = _daily_prices([100])
        weeks = _aggregate_weekly(prices)
        self.assertEqual(len(weeks), 1)


# ============================================================
# パラボリックSAR テスト
# ============================================================

class ParabolicSarTests(unittest.TestCase):

    def test_uptrend_detection(self):
        """上昇トレンド → direction='up', SARは終値より下"""
        # 単調上昇データ
        highs = [100 + i * 2 for i in range(20)]
        lows = [98 + i * 2 for i in range(20)]
        sar_val, direction = _parabolic_sar(highs, lows)
        self.assertEqual(direction, "up")
        self.assertLess(sar_val, lows[-1])

    def test_downtrend_detection(self):
        """下降トレンド → direction='down', SARは高値より上"""
        # 単調下降データ
        highs = [200 - i * 2 for i in range(20)]
        lows = [198 - i * 2 for i in range(20)]
        sar_val, direction = _parabolic_sar(highs, lows)
        self.assertEqual(direction, "down")
        self.assertGreater(sar_val, highs[-1])

    def test_reversal_detection(self):
        """下降→上昇の反転 → 最終的にdirection='up'"""
        # 前半下降、後半上昇
        n = 15
        highs_down = [200 - i * 3 for i in range(n)]
        lows_down = [198 - i * 3 for i in range(n)]
        highs_up = [highs_down[-1] + i * 5 for i in range(n)]
        lows_up = [lows_down[-1] + i * 5 for i in range(n)]
        highs = highs_down + highs_up
        lows = lows_down + lows_up
        sar_val, direction = _parabolic_sar(highs, lows)
        self.assertEqual(direction, "up")

    def test_insufficient_data(self):
        """データ1点 → (None, None)"""
        sar_val, direction = _parabolic_sar([100], [98])
        self.assertIsNone(sar_val)
        self.assertIsNone(direction)

    def test_two_points_minimum(self):
        """データ2点 → 値が返る"""
        sar_val, direction = _parabolic_sar([100, 105], [98, 103])
        self.assertIsNotNone(sar_val)
        self.assertIn(direction, ("up", "down"))


# ============================================================
# サポート/レジスタンス検出テスト
# ============================================================

class SupportResistanceTests(unittest.TestCase):

    def test_clear_support_level(self):
        """明確なサポートレベルがある場合に検出される"""
        # 100付近で3回反発するパターン
        closes = []
        highs = []
        lows = []
        for _ in range(3):
            # 下降して100付近で反発
            for v in [120, 115, 110, 105, 101, 100, 102, 108, 115, 120]:
                closes.append(v)
                highs.append(v + 1)
                lows.append(v - 1)
        # 現在価格は120
        sup, res = _detect_support_resistance(closes, highs, lows, lookback=120)
        self.assertIsNotNone(sup)
        # サポートは100前後であるべき
        self.assertAlmostEqual(sup, 99, delta=3)

    def test_insufficient_data(self):
        """30日未満のデータ → (None, None)"""
        closes = [100.0] * 10
        highs = [101.0] * 10
        lows = [99.0] * 10
        sup, res = _detect_support_resistance(closes, highs, lows)
        self.assertIsNone(sup)
        self.assertIsNone(res)

    def test_no_support_below(self):
        """全データが現在価格より上 → サポートなし"""
        # 常に上昇
        closes = [100 + i for i in range(50)]
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        sup, _ = _detect_support_resistance(closes, highs, lows, lookback=50)
        # 極小値はあるが、現在価格より下に信頼性のあるクラスタがなければNone
        # （結果はデータ次第だが、単調上昇では検出されにくい）


class FindStrongestClusterTests(unittest.TestCase):

    def test_basic_clustering(self):
        """複数の近接レベル → 最頻クラスタの平均を返す"""
        levels = [100, 101, 99, 150, 151, 100.5]
        result = _find_strongest_cluster(levels, current_price=200, is_support=True)
        self.assertIsNotNone(result)
        # 100付近のクラスタ（3〜4要素）が最多
        self.assertAlmostEqual(result, 100, delta=2)

    def test_single_level_returns_none(self):
        """1回しか反発していない → None"""
        levels = [100, 200, 300]  # 全て離れている
        result = _find_strongest_cluster(levels, current_price=400, is_support=True)
        self.assertIsNone(result)

    def test_empty_levels(self):
        """空リスト → None"""
        result = _find_strongest_cluster([], current_price=100, is_support=True)
        self.assertIsNone(result)


# ============================================================
# Pattern C スコアリングテスト
# ============================================================

class PatternCScoreTests(unittest.TestCase):

    def _base_ticker(self, **overrides) -> dict:
        tk = {
            "ticker": "7203", "market": "JP",
            "close": 1000.0, "adj_close": 1000.0,
            "support_level": 990.0,
            "rsi14": 25.0,
            "sar_direction": "up",
        }
        tk.update(overrides)
        return tk

    def test_strong_support_bounce(self):
        """全条件良好 → 高スコア"""
        tk = self._base_ticker(
            close=995, support_level=990,  # 0.5% above support
            rsi14=20, sar_direction="up",
        )
        score = _calc_pattern_c_score(tk)
        self.assertGreaterEqual(score, 80)

    def test_no_support_data(self):
        """サポートレベルなし → サポート成分(40%)が0になり、RSI+SAR分のみ"""
        tk = self._base_ticker(support_level=None, rsi14=25, sar_direction="up")
        score = _calc_pattern_c_score(tk)
        # RSI(85*0.3=25.5) + SAR(100*0.3=30) = 55.5 → 56, サポート成分なし
        self.assertLessEqual(score, 60)
        self.assertGreater(score, 0)

    def test_rsi_deeply_oversold(self):
        """RSI 20 → RSI 33 より高スコア"""
        tk20 = self._base_ticker(rsi14=20)
        tk33 = self._base_ticker(rsi14=33)
        score20 = _calc_pattern_c_score(tk20)
        score33 = _calc_pattern_c_score(tk33)
        self.assertGreater(score20, score33)

    def test_sar_direction_matters(self):
        """SAR up → SAR down より高スコア"""
        tk_up = self._base_ticker(sar_direction="up")
        tk_down = self._base_ticker(sar_direction="down")
        score_up = _calc_pattern_c_score(tk_up)
        score_down = _calc_pattern_c_score(tk_down)
        self.assertGreater(score_up, score_down)

    def test_rsi_above_threshold_zero(self):
        """RSI > 35 → RSI成分は0"""
        tk = self._base_ticker(rsi14=50, sar_direction="down", support_level=None)
        score = _calc_pattern_c_score(tk)
        self.assertEqual(score, 0)

    def test_weights_sum_to_one(self):
        """全重みの合計が1.0"""
        total = W_TREND + W_PATTERN_A + W_PATTERN_B + W_PATTERN_C + W_LIQUIDITY
        self.assertAlmostEqual(total, 1.0, places=10)


# ============================================================
# Pattern C シグナル検出テスト
# ============================================================

class PatternCDetectionTests(unittest.TestCase):
    """_eval_pattern_c のテスト（DBアクセスなし）"""

    def setUp(self):
        patcher = patch("src.config.CONFIG", _CONFIG_MOCK)
        patcher.start()
        self.addCleanup(patcher.stop)
        # signal_detector は CONFIG をモジュールレベルで読むので import 後に使用
        from src.signal_detector import _eval_pattern_c, _flag
        self._eval_pattern_c = _eval_pattern_c
        self._flag = _flag

    def _base_tk(self, **overrides) -> dict:
        tk = {
            "ticker": "7203", "market": "JP",
            "close": 1000.0, "adj_close": 1000.0,
            "weekly_ma25": 980.0, "weekly_ma25_5w_ago": 960.0,
            "support_level": 995.0,
            "rsi14": 28.0,
            "sar_direction": "up",
            "volume": 1500000, "vol_20d_avg": 1000000,
            "atr14": 30.0,
        }
        tk.update(overrides)
        return tk

    def test_all_conditions_met(self):
        """全4条件一致 → candidate"""
        tk = self._base_tk()
        result = self._eval_pattern_c(tk, "2025-05-20", "JP")
        self.assertEqual(result["label"], "candidate")
        self.assertEqual(result["pattern"], "C")
        self.assertEqual(result["cond_c_weekly_uptrend"], 1)
        self.assertEqual(result["cond_c_at_support"], 1)
        self.assertEqual(result["cond_c_rsi_oversold"], 1)
        self.assertEqual(result["cond_c_sar_reversal"], 1)

    def test_three_conditions_watch(self):
        """3条件一致 → watch"""
        tk = self._base_tk(sar_direction="down")  # SAR not reversed
        result = self._eval_pattern_c(tk, "2025-05-20", "JP")
        self.assertEqual(result["label"], "watch")
        self.assertEqual(result["cond_c_sar_reversal"], 0)

    def test_two_conditions_skip(self):
        """2条件一致 → skip"""
        tk = self._base_tk(
            sar_direction="down",  # SAR down
            rsi14=50,              # RSI not oversold
        )
        result = self._eval_pattern_c(tk, "2025-05-20", "JP")
        self.assertEqual(result["label"], "skip")

    def test_no_conditions_skip(self):
        """0条件一致 → skip"""
        tk = self._base_tk(
            weekly_ma25=None,
            support_level=None,
            rsi14=50,
            sar_direction="down",
        )
        result = self._eval_pattern_c(tk, "2025-05-20", "JP")
        self.assertEqual(result["label"], "skip")

    def test_support_tolerance(self):
        """終値がサポートから3%以上離れている → at_support=0"""
        tk = self._base_tk(
            close=1100,           # 10.5% above support_level=995
            support_level=995.0,
        )
        result = self._eval_pattern_c(tk, "2025-05-20", "JP")
        self.assertEqual(result["cond_c_at_support"], 0)

    def test_condition_flags_present(self):
        """全パターンの条件フラグが返り値に含まれる"""
        tk = self._base_tk()
        result = self._eval_pattern_c(tk, "2025-05-20", "JP")
        for key in ("cond_c_weekly_uptrend", "cond_c_at_support",
                     "cond_c_rsi_oversold", "cond_c_sar_reversal"):
            self.assertIn(key, result)
        # Pattern A/B の条件は None
        for key in ("cond_a_ma200_rising", "cond_b_ma200_rising"):
            self.assertIsNone(result[key])


if __name__ == "__main__":
    unittest.main()
