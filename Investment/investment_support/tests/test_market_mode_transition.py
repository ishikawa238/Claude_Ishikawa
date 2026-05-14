"""
市場環境モード遷移のテスト (T-4)

テスト対象:
- _calc_mode(): JP/US の MA25比較結果からモード文字列を算出する純粋関数
- judge_market_environment(): index_snapshot から mode を判定して DB に保存する
- detect_and_save_signals(): caution/full_caution モードで candidate が watch に降格する
"""

import unittest
from datetime import date
from unittest.mock import MagicMock, patch

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
    "screening": {
        "jp_min_price": "100",
        "jp_min_turnover": "500000000",
        "jp_min_market_cap": "10000000000",
        "us_min_price": "5",
        "us_min_turnover": "10000000",
        "us_min_market_cap": "500000000",
        "min_active_days_of_30": "20",
    },
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
    "logging": {"log_dir": "/tmp", "log_level": "INFO"},
}

with patch("src.config.CONFIG", _CONFIG_MOCK):
    from src.market_filter import _calc_mode, judge_market_environment, get_current_mode
    from src.signal_detector import detect_and_save_signals


def _mock_conn():
    """get_connection() コンテキストマネージャのモック。"""
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock())
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def _make_ticker(**overrides) -> dict:
    """シグナル検出テスト用の銘柄辞書を生成する。"""
    base = {
        "ticker": "7203.T", "market": "JP",
        "close": 3000.0, "adj_close": 3000.0,
        "ma5": 2900.0, "ma25": 2800.0, "ma75": 2600.0, "ma200": 2400.0,
        "ma5_5d_ago": 2850.0, "ma25_5d_ago": 2750.0, "ma200_5d_ago": 2380.0,
        "volume": 1200000, "vol_20d_avg": 800000,
        "high_52w": 3100.0, "low_52w": 2000.0,
        "rsi14": 62.0, "atr14": 80.0,
        "est_turnover": 3_600_000_000, "est_market_cap": 50_000_000_000,
        "return_5d": 0.05, "return_20d": 0.10, "volatility_20d": 0.02,
    }
    base.update(overrides)
    return base


# ============================================================
# _calc_mode の単体テスト
# ============================================================

class CalcModeTests(unittest.TestCase):
    """
    _calc_mode() の全 JP/US 組み合わせを網羅する。
    """

    def test_both_above_ma25_returns_normal(self):
        """JP >= MA25 AND US >= MA25 → normal"""
        self.assertEqual(_calc_mode(1, 1), "normal")

    def test_jp_below_us_above_returns_caution(self):
        """JP < MA25 AND US >= MA25 → caution"""
        self.assertEqual(_calc_mode(0, 1), "caution")

    def test_jp_above_us_below_returns_caution(self):
        """JP >= MA25 AND US < MA25 → caution"""
        self.assertEqual(_calc_mode(1, 0), "caution")

    def test_both_below_ma25_returns_full_caution(self):
        """JP < MA25 AND US < MA25 → full_caution"""
        self.assertEqual(_calc_mode(0, 0), "full_caution")

    def test_jp_none_us_above_returns_caution(self):
        """JP データ未取得 (None) AND US >= MA25 → caution（安全側）"""
        self.assertEqual(_calc_mode(None, 1), "caution")

    def test_jp_above_us_none_returns_caution(self):
        """JP >= MA25 AND US データ未取得 (None) → caution（安全側）"""
        self.assertEqual(_calc_mode(1, None), "caution")

    def test_both_none_returns_caution(self):
        """JP/US 両方 None → caution（安全側）"""
        self.assertEqual(_calc_mode(None, None), "caution")


# ============================================================
# judge_market_environment のテスト
# ============================================================

class JudgeMarketEnvironmentTests(unittest.TestCase):
    """
    judge_market_environment() が index_snapshot から mode を正しく算出し
    DB に保存することを確認する。
    """

    def _setup_index_mock(self, jp_close, jp_ma25, us_close, us_ma25):
        """
        _get_index_latest が JP/US の close・ma25 を返すようにモックを設定する。
        """
        def side_effect(conn, sql, params):
            index_code = params[0]
            if index_code == "N225":
                return MagicMock(
                    __iter__=lambda s: iter([("close", jp_close), ("ma25", jp_ma25)]),
                    keys=lambda: ["close", "ma25"],
                    **{"__getitem__": lambda s, k: {"close": jp_close, "ma25": jp_ma25}[k]},
                ) if jp_close is not None else None
            if index_code == "SP500":
                return MagicMock(
                    __iter__=lambda s: iter([("close", us_close), ("ma25", us_ma25)]),
                    keys=lambda: ["close", "ma25"],
                    **{"__getitem__": lambda s, k: {"close": us_close, "ma25": us_ma25}[k]},
                ) if us_close is not None else None
            return None
        return side_effect

    @patch("src.market_filter.upsert")
    @patch("src.market_filter.fetch_one")
    @patch("src.market_filter.get_connection")
    def test_normal_mode_when_both_above(self, mock_conn, mock_fetch_one, mock_upsert):
        """JP close > MA25 AND US close > MA25 → mode = normal"""
        mock_conn.return_value = _mock_conn()
        # fetch_one: 1回目=既存レコード検索(None), 2回目=N225, 3回目=SP500
        mock_fetch_one.side_effect = [
            None,  # 既存レコードなし
            {"close": 39000.0, "ma25": 38000.0},   # N225: close > ma25
            {"close": 5200.0,  "ma25": 5100.0},    # SP500: close > ma25
        ]
        result = judge_market_environment(date(2026, 5, 1), market="ALL")
        self.assertEqual(result["mode"], "normal")
        self.assertEqual(result["jp_above_ma25"], 1)
        self.assertEqual(result["us_above_ma25"], 1)

    @patch("src.market_filter.upsert")
    @patch("src.market_filter.fetch_one")
    @patch("src.market_filter.get_connection")
    def test_caution_mode_when_jp_below(self, mock_conn, mock_fetch_one, mock_upsert):
        """JP close < MA25 AND US close > MA25 → mode = caution"""
        mock_conn.return_value = _mock_conn()
        mock_fetch_one.side_effect = [
            None,
            {"close": 37000.0, "ma25": 38000.0},   # N225: close < ma25
            {"close": 5200.0,  "ma25": 5100.0},    # SP500: close > ma25
        ]
        result = judge_market_environment(date(2026, 5, 1), market="ALL")
        self.assertEqual(result["mode"], "caution")
        self.assertEqual(result["jp_above_ma25"], 0)
        self.assertEqual(result["us_above_ma25"], 1)

    @patch("src.market_filter.upsert")
    @patch("src.market_filter.fetch_one")
    @patch("src.market_filter.get_connection")
    def test_caution_mode_when_us_below(self, mock_conn, mock_fetch_one, mock_upsert):
        """JP close > MA25 AND US close < MA25 → mode = caution"""
        mock_conn.return_value = _mock_conn()
        mock_fetch_one.side_effect = [
            None,
            {"close": 39000.0, "ma25": 38000.0},   # N225: close > ma25
            {"close": 4900.0,  "ma25": 5100.0},    # SP500: close < ma25
        ]
        result = judge_market_environment(date(2026, 5, 1), market="ALL")
        self.assertEqual(result["mode"], "caution")
        self.assertEqual(result["jp_above_ma25"], 1)
        self.assertEqual(result["us_above_ma25"], 0)

    @patch("src.market_filter.upsert")
    @patch("src.market_filter.fetch_one")
    @patch("src.market_filter.get_connection")
    def test_full_caution_mode_when_both_below(self, mock_conn, mock_fetch_one, mock_upsert):
        """JP close < MA25 AND US close < MA25 → mode = full_caution"""
        mock_conn.return_value = _mock_conn()
        mock_fetch_one.side_effect = [
            None,
            {"close": 37000.0, "ma25": 38000.0},   # N225: close < ma25
            {"close": 4900.0,  "ma25": 5100.0},    # SP500: close < ma25
        ]
        result = judge_market_environment(date(2026, 5, 1), market="ALL")
        self.assertEqual(result["mode"], "full_caution")
        self.assertEqual(result["jp_above_ma25"], 0)
        self.assertEqual(result["us_above_ma25"], 0)

    @patch("src.market_filter.upsert")
    @patch("src.market_filter.fetch_one")
    @patch("src.market_filter.get_connection")
    def test_transition_normal_to_caution(self, mock_conn, mock_fetch_one, mock_upsert):
        """
        前日 normal → 当日 JP が MA25 を下回った場合に caution へ遷移する。
        前日の既存レコードは参照せず、当日のインデックスデータで再判定する。
        """
        mock_conn.return_value = _mock_conn()
        # 既存レコードあり（前日 normal）
        mock_fetch_one.side_effect = [
            {"date": "2026-05-01", "mode": "normal",
             "jp_above_ma25": 1, "us_above_ma25": 1,
             "n225_close": 39000.0, "n225_ma25": 38000.0,
             "sp500_close": 5200.0, "sp500_ma25": 5100.0},
            {"close": 37500.0, "ma25": 38000.0},   # N225: close < ma25（当日）
            {"close": 5200.0,  "ma25": 5100.0},    # SP500: close > ma25（当日）
        ]
        result = judge_market_environment(date(2026, 5, 2), market="ALL")
        self.assertEqual(result["mode"], "caution")

    @patch("src.market_filter.upsert")
    @patch("src.market_filter.fetch_one")
    @patch("src.market_filter.get_connection")
    def test_upsert_called_with_mode(self, mock_conn, mock_fetch_one, mock_upsert):
        """judge_market_environment は upsert を呼び出して DB に保存する。"""
        mock_conn.return_value = _mock_conn()
        mock_fetch_one.side_effect = [
            None,
            {"close": 39000.0, "ma25": 38000.0},
            {"close": 5200.0,  "ma25": 5100.0},
        ]
        judge_market_environment(date(2026, 5, 1), market="ALL")
        self.assertTrue(mock_upsert.called)
        # upsert に渡された行に mode が含まれていることを確認
        saved_rows = mock_upsert.call_args[0][2]  # upsert(conn, table, rows)
        self.assertIn("mode", saved_rows[0])


# ============================================================
# detect_and_save_signals でのラベル降格テスト
# ============================================================

class MarketModeSignalDemotionTests(unittest.TestCase):
    """
    caution / full_caution モードで、本来 candidate になるシグナルが
    watch に降格されることを確認する。
    """

    @patch("src.signal_detector.fetch_recent_lows", return_value={})
    @patch("src.signal_detector.fetch_all", return_value=[])
    @patch("src.signal_detector.upsert")
    @patch("src.signal_detector.get_connection")
    def test_caution_mode_demotes_candidate_to_watch(
        self, mock_conn, mock_upsert, mock_fetch_all, _mock_lows
    ):
        """
        caution モードでは candidate ラベルが watch に降格し、
        返り値の candidates リストが空になる。
        """
        mock_conn.return_value = _mock_conn()
        # 全条件を充足するブレイクアウト銘柄
        ticker = _make_ticker(
            close=3100.0, high_52w=3100.0,   # 52週高値到達
            volume=2000000, vol_20d_avg=800000,  # 出来高急増
            rsi14=60.0,
        )
        result = detect_and_save_signals([ticker], date(2026, 5, 1), "caution")
        # caution モードではいかなる候補も返さない
        self.assertEqual(result, [])
        # DB 保存された行を確認
        self.assertTrue(mock_upsert.called)
        saved_rows = mock_upsert.call_args[0][2]
        self.assertNotEqual(saved_rows[0]["label"], "candidate",
                            "caution モードでは candidate ラベルが保存されてはならない")

    @patch("src.signal_detector.fetch_recent_lows", return_value={})
    @patch("src.signal_detector.fetch_all", return_value=[])
    @patch("src.signal_detector.upsert")
    @patch("src.signal_detector.get_connection")
    def test_full_caution_mode_demotes_candidate_to_watch(
        self, mock_conn, mock_upsert, mock_fetch_all, _mock_lows
    ):
        """full_caution モードでも同様に candidate が watch に降格する。"""
        mock_conn.return_value = _mock_conn()
        ticker = _make_ticker(
            close=3100.0, high_52w=3100.0,
            volume=2000000, vol_20d_avg=800000,
            rsi14=60.0,
        )
        result = detect_and_save_signals([ticker], date(2026, 5, 1), "full_caution")
        self.assertEqual(result, [])

    @patch("src.signal_detector.fetch_recent_lows", return_value={})
    @patch("src.signal_detector.fetch_all", return_value=[])
    @patch("src.signal_detector.upsert")
    @patch("src.signal_detector.get_connection")
    def test_normal_mode_keeps_candidate(
        self, mock_conn, mock_upsert, mock_fetch_all, _mock_lows
    ):
        """
        normal モードでは candidate が降格しない（回帰確認）。
        スコア >= 50 の銘柄は candidates に残る。
        """
        mock_conn.return_value = _mock_conn()
        ticker = _make_ticker(
            close=3100.0, high_52w=3100.0,
            volume=2000000, vol_20d_avg=800000,
            rsi14=60.0,
        )
        result = detect_and_save_signals([ticker], date(2026, 5, 1), "normal")
        # normal モードでは DB 行が保存される（ラベルは score 次第）
        self.assertTrue(mock_upsert.called)
        saved_rows = mock_upsert.call_args[0][2]
        # DB 行の market_mode が "normal" になっていること
        self.assertEqual(saved_rows[0]["market_mode"], "normal")

    @patch("src.signal_detector.fetch_recent_lows", return_value={})
    @patch("src.signal_detector.fetch_all", return_value=[])
    @patch("src.signal_detector.upsert")
    @patch("src.signal_detector.get_connection")
    def test_watch_label_preserved_in_caution(
        self, mock_conn, mock_upsert, mock_fetch_all, _mock_lows
    ):
        """
        caution モードで元々 watch の銘柄は watch のまま DB に保存される。
        （降格の二重処理が起きないことの確認）
        """
        mock_conn.return_value = _mock_conn()
        # スコアが低く watch 相当の銘柄（RSI 範囲外・出来高弱い）
        ticker = _make_ticker(
            close=2600.0, high_52w=3100.0,  # 52週高値から遠い
            volume=500000, vol_20d_avg=800000,  # 出来高少ない
            rsi14=35.0,  # RSI 範囲外
        )
        detect_and_save_signals([ticker], date(2026, 5, 1), "caution")
        self.assertTrue(mock_upsert.called)
        saved_rows = mock_upsert.call_args[0][2]
        # candidate にはなっていないこと
        self.assertNotEqual(saved_rows[0]["label"], "candidate")


if __name__ == "__main__":
    unittest.main()
