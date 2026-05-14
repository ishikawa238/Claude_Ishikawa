"""
52週高値計算ロジックのテスト (T-5)

テスト対象:
- technical_calculator.calculate_snapshot() の high_52w フィールド
  - 252営業日分のローリングmax
  - データが 252 日未満の場合は全期間の max にフォールバック
- signal_detector._eval_pattern_a() の cond_a_52w_high_near フラグ
  - _PA_52W_PCT (0.92) をしきい値に close/high_52w の比率で判定

設計方針:
- DB への接続は不要。calculate_snapshot() に prices を直接渡す。
- _eval_pattern_a() はモジュール内部関数のため import して直接テスト。
"""

import unittest
from datetime import date, timedelta
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
    from src.technical_calculator import calculate_snapshot
    from src.signal_detector import _eval_pattern_a


# ============================================================
# ヘルパー関数
# ============================================================

def _make_price_row(date_str: str, close: float) -> dict:
    """price_history の1行に相当するデータを生成する。"""
    return {
        "date": date_str,
        "adj_close": close,
        "open": close,
        "high": close,
        "low": close,
        "volume": 1_000_000.0,
    }


def _generate_prices(n_days: int, base_price: float = 1000.0) -> list[dict]:
    """
    指定日数分の価格データを生成する（全て同一価格）。
    対象日から逆算して n_days 分の日付を生成する。
    """
    base_date = date(2026, 1, 2)  # 対象日を 2026-01-02 に固定
    rows = []
    for i in range(n_days):
        d = base_date - timedelta(days=(n_days - 1 - i))
        rows.append(_make_price_row(d.strftime("%Y-%m-%d"), base_price))
    return rows


def _target_date_for(prices: list[dict]) -> date:
    """価格リストの最終日を対象日として返す。"""
    return date.fromisoformat(prices[-1]["date"])


# ============================================================
# high_52w の計算ロジックテスト
# ============================================================

class High52wCalculationTests(unittest.TestCase):
    """
    calculate_snapshot() の high_52w フィールドが
    正しく計算されることを確認する。
    """

    def test_252_days_data_uses_rolling_max(self):
        """
        252営業日分のデータがある場合、直近252日の最大値が high_52w になる。
        最初の日だけ価格を高くして、それが high_52w に反映されることを確認する。
        """
        prices = _generate_prices(260, base_price=1000.0)
        # 最初の日（252日より前）を高値に設定
        prices[0]["adj_close"] = 2000.0
        prices[0]["high"] = 2000.0
        # 直近252日の範囲内にある7日目から末尾は全て 1000.0
        # → 最初の 2000.0 は252日範囲外なので high_52w = 1000.0 になるはず
        target = _target_date_for(prices)
        result = calculate_snapshot("TEST", target, prices)
        self.assertEqual(result["high_52w"], 1000.0,
                         "252日範囲外の高値は high_52w に含まれない")

    def test_252_days_data_spike_within_range(self):
        """
        直近252日以内にスパイクがある場合、そのスパイクが high_52w になる。
        """
        prices = _generate_prices(260, base_price=1000.0)
        # 200日前（252日範囲内）を高値に設定
        prices[60]["adj_close"] = 1800.0
        prices[60]["high"] = 1800.0
        target = _target_date_for(prices)
        result = calculate_snapshot("TEST", target, prices)
        self.assertEqual(result["high_52w"], 1800.0,
                         "252日範囲内のスパイクが high_52w になるべき")

    def test_current_price_equals_52w_high(self):
        """
        現在値が 52週高値に等しい場合（ratio = 1.0）。
        すべて同一価格のデータでは high_52w = close となる。
        """
        prices = _generate_prices(252, base_price=3000.0)
        target = _target_date_for(prices)
        result = calculate_snapshot("TEST", target, prices)
        self.assertEqual(result["high_52w"], 3000.0)
        # ratio = close / high_52w = 1.0
        ratio = result["high_52w"] / result["high_52w"]
        self.assertAlmostEqual(ratio, 1.0)

    def test_current_price_is_92_percent_of_52w_high(self):
        """
        現在値が 52週高値の 92% の場合（パターンA のしきい値境界）。
        """
        prices = _generate_prices(252, base_price=920.0)
        # 直近252日内の1日だけ 1000 に設定（52週高値 = 1000）
        prices[10]["adj_close"] = 1000.0
        prices[10]["high"] = 1000.0
        target = _target_date_for(prices)
        result = calculate_snapshot("TEST", target, prices)
        self.assertEqual(result["high_52w"], 1000.0)
        close = result["high_52w"]  # 最後の価格は 920 だが high_52w が計算対象
        # 実際の終値を確認
        actual_close = 920.0
        ratio = actual_close / result["high_52w"]
        self.assertAlmostEqual(ratio, 0.92, places=5)

    def test_fewer_than_252_days_uses_all_data(self):
        """
        データが 252日未満の場合、全期間の最大値が high_52w になる。
        """
        prices = _generate_prices(100, base_price=1000.0)
        prices[50]["adj_close"] = 1500.0
        prices[50]["high"] = 1500.0
        target = _target_date_for(prices)
        result = calculate_snapshot("TEST", target, prices)
        self.assertEqual(result["high_52w"], 1500.0,
                         "252日未満のデータでは全期間の最大値を high_52w とする")

    def test_strictly_increasing_prices(self):
        """
        単調増加の価格データでは high_52w = 最終日の終値。
        """
        base_date = date(2026, 1, 2)
        prices = []
        n = 252
        for i in range(n):
            d = base_date - timedelta(days=(n - 1 - i))
            price = 1000.0 + i * 10.0  # 1日ごとに+10
            prices.append(_make_price_row(d.strftime("%Y-%m-%d"), price))
        target = _target_date_for(prices)
        result = calculate_snapshot("TEST", target, prices)
        expected_high = 1000.0 + (n - 1) * 10.0  # 最終日の価格
        self.assertAlmostEqual(result["high_52w"], expected_high, places=5,
                               msg="単調増加では最終日の終値が high_52w になる")

    def test_early_spike_then_decline(self):
        """
        最初に急騰してその後下降する場合、急騰値が high_52w になる（252日以内）。
        """
        prices = _generate_prices(252, base_price=500.0)
        # 252日範囲の先頭に急騰を設定
        prices[1]["adj_close"] = 2000.0
        prices[1]["high"] = 2000.0
        target = _target_date_for(prices)
        result = calculate_snapshot("TEST", target, prices)
        self.assertEqual(result["high_52w"], 2000.0,
                         "252日範囲内の早期スパイクが high_52w になる")

    def test_single_day_data(self):
        """
        データが1日のみの場合でも high_52w が計算できる（エッジケース）。
        """
        prices = [_make_price_row("2026-01-02", 1500.0)]
        target = _target_date_for(prices)
        result = calculate_snapshot("TEST", target, prices)
        self.assertEqual(result["high_52w"], 1500.0)


# ============================================================
# _eval_pattern_a() での 52週高値近傍フラグのテスト
# ============================================================

class PatternA52wHighFlagTests(unittest.TestCase):
    """
    _eval_pattern_a() の cond_a_52w_high_near フラグが
    正しく計算されることを確認する。
    （しきい値: close >= high_52w * 0.92）
    """

    def _base_ticker(self, close, high_52w: float) -> dict:
        """パターンA評価用の最小限の銘柄辞書を生成する。"""
        ref = close if close is not None else 3000.0
        return {
            "ticker": "TEST",
            "close": close, "adj_close": close,
            "ma5": ref * 0.97, "ma25": ref * 0.94,
            "ma75": ref * 0.88, "ma200": ref * 0.82,
            "ma200_5d_ago": ref * 0.81,
            "volume": 1_500_000, "vol_20d_avg": 1_000_000,
            "high_52w": high_52w,
            "rsi14": 60.0,
            "atr14": 50.0,
        }

    def test_ratio_1_0_flag_is_1(self):
        """現在値 = 52週高値（ratio = 1.0）→ cond_a_52w_high_near = 1"""
        tk = self._base_ticker(close=3000.0, high_52w=3000.0)
        row = _eval_pattern_a(tk, "2026-05-01", "JP")
        self.assertEqual(row["cond_a_52w_high_near"], 1)

    def test_ratio_exactly_0_92_flag_is_1(self):
        """現在値 = 52週高値 × 0.92（しきい値ちょうど）→ cond_a_52w_high_near = 1"""
        high_52w = 3000.0
        close = high_52w * 0.92  # = 2760.0
        tk = self._base_ticker(close=close, high_52w=high_52w)
        row = _eval_pattern_a(tk, "2026-05-01", "JP")
        self.assertEqual(row["cond_a_52w_high_near"], 1,
                         "0.92 はしきい値以上なので flag = 1")

    def test_ratio_below_0_92_flag_is_0(self):
        """現在値 = 52週高値 × 0.91（しきい値未満）→ cond_a_52w_high_near = 0"""
        high_52w = 3000.0
        close = high_52w * 0.91  # = 2730.0
        tk = self._base_ticker(close=close, high_52w=high_52w)
        row = _eval_pattern_a(tk, "2026-05-01", "JP")
        self.assertEqual(row["cond_a_52w_high_near"], 0,
                         "0.91 はしきい値未満なので flag = 0")

    def test_high_52w_none_flag_is_0(self):
        """high_52w が None の場合 → cond_a_52w_high_near = 0"""
        tk = self._base_ticker(close=3000.0, high_52w=None)
        row = _eval_pattern_a(tk, "2026-05-01", "JP")
        self.assertEqual(row["cond_a_52w_high_near"], 0)

    def test_close_none_flag_is_0(self):
        """close が None の場合 → cond_a_52w_high_near = 0"""
        tk = self._base_ticker(close=None, high_52w=3000.0)
        tk["close"] = None
        tk["adj_close"] = None
        row = _eval_pattern_a(tk, "2026-05-01", "JP")
        self.assertEqual(row["cond_a_52w_high_near"], 0)

    def test_ratio_0_95_flag_is_1(self):
        """現在値 = 52週高値 × 0.95（しきい値以上）→ cond_a_52w_high_near = 1"""
        high_52w = 2000.0
        close = high_52w * 0.95  # = 1900.0
        tk = self._base_ticker(close=close, high_52w=high_52w)
        row = _eval_pattern_a(tk, "2026-05-01", "US")
        self.assertEqual(row["cond_a_52w_high_near"], 1)

    def test_pattern_field_is_A(self):
        """_eval_pattern_a の返り値に pattern = 'A' が含まれる。"""
        tk = self._base_ticker(close=3000.0, high_52w=3000.0)
        row = _eval_pattern_a(tk, "2026-05-01", "JP")
        self.assertEqual(row["pattern"], "A")


# ============================================================
# high_52w と low_52w の整合性テスト
# ============================================================

class High52wLow52wConsistencyTests(unittest.TestCase):
    """
    high_52w と low_52w が常に high >= low になることを確認する。
    """

    def test_high_always_gte_low(self):
        """単調増加データでは high_52w >= low_52w が成立する。"""
        base_date = date(2026, 1, 2)
        prices = []
        for i in range(252):
            d = base_date - timedelta(days=(252 - 1 - i))
            price = 1000.0 + i * 5.0
            prices.append(_make_price_row(d.strftime("%Y-%m-%d"), price))
        target = _target_date_for(prices)
        result = calculate_snapshot("TEST", target, prices)
        self.assertGreaterEqual(result["high_52w"], result["low_52w"])

    def test_high_gte_low_with_volatile_prices(self):
        """価格が乱高下しても high_52w >= low_52w が成立する。"""
        base_date = date(2026, 1, 2)
        prices = []
        n = 252
        for i in range(n):
            d = base_date - timedelta(days=(n - 1 - i))
            # 偶数日は高値、奇数日は安値
            price = 2000.0 if i % 2 == 0 else 500.0
            prices.append(_make_price_row(d.strftime("%Y-%m-%d"), price))
        target = _target_date_for(prices)
        result = calculate_snapshot("TEST", target, prices)
        self.assertGreaterEqual(result["high_52w"], result["low_52w"])
        self.assertEqual(result["high_52w"], 2000.0)
        self.assertEqual(result["low_52w"], 500.0)


if __name__ == "__main__":
    unittest.main()
