"""
signal_detector モジュールのテスト

テスト対象:
- detect_and_save_signals(): 市場環境モードに関わらず検出を実施すること
- スコアリング統合: upsert に渡される行に final_score 等が含まれること
- ラベル昇格: final_score >= 50 の watch → candidate 昇格
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
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
    "logging": {"log_dir": "/tmp", "log_level": "INFO"},
}

with patch("src.config.CONFIG", _CONFIG_MOCK):
    from src.signal_detector import detect_and_save_signals


class DetectAndSaveSignalsMarketModeTests(unittest.TestCase):
    """
    caution / full_caution モードでもシグナル検出がスキップされないことを確認する。
    銘柄リストが空なら検出結果も空リストになる（スキップではなく、対象なし）。
    """

    def _mock_conn(self):
        """get_connection() コンテキストマネージャのモック。"""
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=MagicMock())
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    @patch("src.signal_detector.upsert")
    @patch("src.signal_detector.get_connection")
    def test_caution_mode_runs_detection(self, mock_conn, mock_upsert):
        """caution モードで screened_tickers が空のとき [] を返す（早期リターンではない）。"""
        mock_conn.return_value = self._mock_conn()
        result = detect_and_save_signals([], date(2026, 4, 22), "caution")
        self.assertEqual(result, [])
        # upsert は0件なので呼ばれない（DBエラーにはならない）

    @patch("src.signal_detector.upsert")
    @patch("src.signal_detector.get_connection")
    def test_full_caution_mode_runs_detection(self, mock_conn, mock_upsert):
        """full_caution モードでも同様に [] を返す（スキップではない）。"""
        mock_conn.return_value = self._mock_conn()
        result = detect_and_save_signals([], date(2026, 4, 22), "full_caution")
        self.assertEqual(result, [])

    @patch("src.signal_detector.fetch_recent_lows", return_value={})
    @patch("src.signal_detector.fetch_all")
    @patch("src.signal_detector.upsert")
    @patch("src.signal_detector.get_connection")
    def test_caution_mode_evaluates_ticker(self, mock_conn, mock_upsert, mock_fetch_all, _mock_lows):
        """caution モードでも銘柄の評価ループが実行され、DB保存が呼ばれること。"""
        mock_conn.return_value = self._mock_conn()
        mock_fetch_all.return_value = []  # _check_pullback の price_history 問い合わせ結果

        ticker = {
            "ticker": "7203.T", "market": "JP",
            "close": 3000.0, "adj_close": 3000.0,
            "ma5": 2900.0, "ma25": 2800.0, "ma75": 2600.0, "ma200": 2400.0,
            "ma5_5d_ago": 2850.0, "ma25_5d_ago": 2750.0, "ma200_5d_ago": 2380.0,
            "volume": 1000000, "vol_20d_avg": 700000,
            "high_52w": 3100.0, "low_52w": 2000.0,
            "rsi14": 58.0, "atr14": 80.0,
            "est_turnover": 3000000000, "est_market_cap": 50000000000,
            "return_5d": 0.05, "return_20d": 0.10, "volatility_20d": 0.02,
        }
        result = detect_and_save_signals([ticker], date(2026, 4, 22), "caution")
        # upsert が1回以上呼ばれること（評価ループが動いた証拠）
        self.assertTrue(mock_upsert.called, "caution モードでも upsert が呼ばれるべき")

    @patch("src.signal_detector.fetch_recent_lows", return_value={})
    @patch("src.signal_detector.fetch_all")
    @patch("src.signal_detector.upsert")
    @patch("src.signal_detector.get_connection")
    def test_normal_mode_evaluates_ticker(self, mock_conn, mock_upsert, mock_fetch_all, _mock_lows):
        """normal モードでの挙動が caution モードと同じであること（回帰確認）。"""
        mock_conn.return_value = self._mock_conn()
        mock_fetch_all.return_value = []

        ticker = {
            "ticker": "AAPL", "market": "US",
            "close": 200.0, "adj_close": 200.0,
            "ma5": 195.0, "ma25": 190.0, "ma75": 180.0, "ma200": 170.0,
            "ma5_5d_ago": 192.0, "ma25_5d_ago": 188.0, "ma200_5d_ago": 168.0,
            "volume": 50000000, "vol_20d_avg": 30000000,
            "high_52w": 210.0, "low_52w": 140.0,
            "rsi14": 60.0, "atr14": 5.0,
            "est_turnover": 10000000000, "est_market_cap": 3000000000000,
            "return_5d": 0.03, "return_20d": 0.08, "volatility_20d": 0.015,
        }
        result = detect_and_save_signals([ticker], date(2026, 4, 22), "normal")
        self.assertTrue(mock_upsert.called)


class ScoringIntegrationTests(unittest.TestCase):
    """upsert に渡される行にスコアフィールドが含まれることを確認する。"""

    def _mock_conn(self):
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=MagicMock())
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    @patch("src.signal_detector.fetch_recent_lows", return_value={})
    @patch("src.signal_detector.fetch_all")
    @patch("src.signal_detector.upsert")
    @patch("src.signal_detector.get_connection")
    def test_upsert_rows_contain_score_fields(self, mock_conn, mock_upsert, mock_fetch_all, _mock_lows):
        """upsert に渡される行辞書に final_score / recommend_rank 等が存在する。"""
        mock_conn.return_value = self._mock_conn()
        mock_fetch_all.return_value = []

        ticker = {
            "ticker": "9984", "market": "JP",
            "close": 8000.0, "adj_close": 8000.0,
            "ma5": 7900.0, "ma25": 7700.0, "ma75": 7500.0, "ma200": 7000.0,
            "ma5_5d_ago": 7800.0, "ma25_5d_ago": 7600.0, "ma200_5d_ago": 6900.0,
            "volume": 20000000, "vol_20d_avg": 12000000,
            "high_52w": 8200.0, "low_52w": 5000.0,
            "rsi14": 62.0, "atr14": 200.0,
            "est_turnover": 160_000_000_000, "est_market_cap": 12_000_000_000_000,
        }
        detect_and_save_signals([ticker], date(2026, 4, 22), "normal")

        # upsert の引数からDB保存行を取り出す
        self.assertTrue(mock_upsert.called)
        call_args = mock_upsert.call_args_list[0]
        rows = call_args[0][2]  # upsert(conn, table, rows) の 3 番目
        self.assertEqual(len(rows), 1)

        row = rows[0]
        for key in ("final_score", "recommend_rank", "recommend_reason",
                     "trend_score", "pattern_a_score", "pattern_b_score",
                     "liquidity_score", "base_score", "market_multiplier",
                     "market_mode"):
            self.assertIn(key, row, f"'{key}' が行に含まれていない")

    @patch("src.signal_detector.fetch_recent_lows", return_value={})
    @patch("src.signal_detector.fetch_all")
    @patch("src.signal_detector.upsert")
    @patch("src.signal_detector.get_connection")
    def test_high_score_watch_promoted_to_candidate(self, mock_conn, mock_upsert, mock_fetch_all, _mock_lows):
        """既存条件では watch だが final_score >= 50 なら candidate に昇格する。"""
        mock_conn.return_value = self._mock_conn()
        mock_fetch_all.return_value = []

        # MA整列・52w近傍・出来高で高スコアだが、1〜2条件不足で既存ラベルは watch
        ticker = {
            "ticker": "6758", "market": "JP",
            "close": 3000.0, "adj_close": 3000.0,
            "ma5": 2980.0, "ma25": 2900.0, "ma75": 2800.0, "ma200": 2600.0,
            "ma5_5d_ago": 2950.0, "ma25_5d_ago": 2870.0, "ma200_5d_ago": 2560.0,
            "volume": 2000000, "vol_20d_avg": 1000000,
            "high_52w": 3100.0, "low_52w": 2000.0,
            "rsi14": 60.0, "atr14": 80.0,
            "est_turnover": 6_000_000_000, "est_market_cap": 50_000_000_000,
        }
        result = detect_and_save_signals([ticker], date(2026, 4, 22), "normal")

        # upsert 行を確認
        call_args = mock_upsert.call_args_list[0]
        rows = call_args[0][2]
        row = rows[0]

        # final_score が計算されていること
        self.assertIsNotNone(row.get("final_score"))
        # スコアが 50 以上なら candidate に昇格しているはず
        if row["final_score"] >= 50:
            self.assertEqual(row["label"], "candidate")
            self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main()
