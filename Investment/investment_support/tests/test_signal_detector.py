"""
signal_detector モジュールのテスト

テスト対象:
- detect_and_save_signals(): 市場環境モードに関わらず検出を実施すること
- スコアリング統合: upsert に渡される行に final_score 等が含まれること
- ラベル昇格: final_score と主要パターンスコアを満たす watch → score candidate 昇格
"""

import unittest
from datetime import date
from unittest.mock import MagicMock, patch

_CONFIG_MOCK = {
    "signal": {
        "max_entry_sets_per_batch": "3",
    },
    "swing": {
        "us_min_price": 10,
        "us_max_price": 50,
        "jp_min_price": 300,
        "jp_max_price": 3000,
        "ema_short": 25,
        "ema_long": 75,
        "rsi_max": 35,
        "rsi_lookback_days": 5,
        "risk_reward_ratio": 2.0,
        "max_risk_per_trade_pct": 2.0,
        "support_stop_buffer_pct": 1.0,
    },
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
    "logging": {"log_dir": "/tmp", "log_level": "INFO"},
}

with patch("src.config.CONFIG", _CONFIG_MOCK):
    from src.signal_detector import (
        _max_entry_sets_per_batch,
        diagnose_signal_save_count,
        detect_and_save_signals,
    )


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

    @patch("src.signal_detector.fetch_all")
    @patch("src.signal_detector.upsert")
    @patch("src.signal_detector.get_connection")
    def test_caution_mode_evaluates_ticker(self, mock_conn, mock_upsert, mock_fetch_all):
        """caution モードでも銘柄の評価ループが実行され、DB保存が呼ばれること。"""
        mock_conn.return_value = self._mock_conn()
        mock_fetch_all.return_value = []

        ticker = {
            "ticker": "7203", "market": "JP", "name": "トヨタ",
            "close": 3000.0,
            "ema25": 3100.0, "ema75": 2900.0,
            "rsi14": 32.0, "atr14": 80.0,
            "volume": 1000000, "vol_20d_avg": 700000,
            "support_level": 2850.0,
            "macd_diff": 10.0, "macd_dea": 5.0, "macd_hist": 5.0,
            "macd_diff_prev": -2.0, "macd_dea_prev": 4.0, "macd_hist_prev": -1.0,
            "est_turnover": 3000000000, "est_market_cap": 50000000000,
        }
        detect_and_save_signals([ticker], date(2026, 4, 22), "caution")
        self.assertTrue(mock_upsert.called, "caution モードでも upsert が呼ばれるべき")

    @patch("src.signal_detector.fetch_all")
    @patch("src.signal_detector.upsert")
    @patch("src.signal_detector.get_connection")
    def test_normal_mode_evaluates_ticker(self, mock_conn, mock_upsert, mock_fetch_all):
        """normal モードで candidate が返ること。"""
        mock_conn.return_value = self._mock_conn()
        mock_fetch_all.return_value = []

        ticker = {
            "ticker": "PLTR", "market": "US", "name": "Palantir",
            "close": 25.0,
            "ema25": 30.0, "ema75": 28.0,
            "rsi14": 32.0, "atr14": 1.5,
            "volume": 50000000, "vol_20d_avg": 30000000,
            "support_level": 23.0,
            "macd_diff": 0.2, "macd_dea": 0.1, "macd_hist": 0.1,
            "macd_diff_prev": -0.1, "macd_dea_prev": 0.0, "macd_hist_prev": -0.1,
            "est_turnover": 10000000000, "est_market_cap": 3000000000000,
        }
        result = detect_and_save_signals([ticker], date(2026, 4, 22), "normal")
        self.assertTrue(mock_upsert.called)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["label"], "candidate")


class SwingIntegrationTests(unittest.TestCase):
    """スイング条件が daily_signals 行に反映されることを確認する。"""

    def _mock_conn(self):
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=MagicMock())
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    @patch("src.signal_detector.is_breakout_enabled", return_value=False)
    @patch("src.signal_detector.fetch_all")
    @patch("src.signal_detector.upsert")
    @patch("src.signal_detector.get_connection")
    def test_upsert_rows_contain_swing_fields(
        self, mock_conn, mock_upsert, mock_fetch_all, _mock_breakout,
    ):
        mock_conn.return_value = self._mock_conn()
        mock_fetch_all.return_value = []
        ticker = {
            "ticker": "PLTR", "market": "US", "name": "Palantir",
            "close": 25.0, "ema25": 30.0, "ema75": 28.0, "rsi14": 32.0, "atr14": 1.5,
            "volume": 1e6, "vol_20d_avg": 8e5, "support_level": 23.0,
            "macd_diff": 0.2, "macd_dea": 0.1, "macd_hist": 0.1,
            "macd_diff_prev": -0.1, "macd_dea_prev": 0.0, "macd_hist_prev": -0.1,
            "est_turnover": 2e7, "est_market_cap": 5e9,
        }
        detect_and_save_signals([ticker], date(2026, 4, 22), "normal")
        rows = mock_upsert.call_args_list[0][0][2]
        row = rows[0]
        self.assertEqual(row["cond_c_weekly_uptrend"], 1)
        self.assertEqual(row["cond_c_rsi_oversold"], 1)
        self.assertEqual(row["cond_c_sar_reversal"], 1)
        self.assertEqual(row["macd_confirm_type"], "both")
        self.assertEqual(row["label"], "candidate")

    @patch("src.signal_detector.is_breakout_enabled", return_value=False)
    @patch("src.signal_detector.fetch_all")
    @patch("src.signal_detector.upsert")
    @patch("src.signal_detector.get_connection")
    def test_caution_demotes_candidate_to_watch(
        self, mock_conn, mock_upsert, mock_fetch_all, _mock_breakout,
    ):
        mock_conn.return_value = self._mock_conn()
        mock_fetch_all.return_value = []
        ticker = {
            "ticker": "PLTR", "market": "US", "name": "Palantir",
            "close": 25.0, "ema25": 30.0, "ema75": 28.0, "rsi14": 32.0, "atr14": 1.5,
            "volume": 1e6, "vol_20d_avg": 8e5, "support_level": 23.0,
            "macd_diff": 0.2, "macd_dea": 0.1, "macd_hist": 0.1,
            "macd_diff_prev": -0.1, "macd_dea_prev": 0.0, "macd_hist_prev": -0.1,
            "est_turnover": 2e7, "est_market_cap": 5e9,
        }
        result = detect_and_save_signals([ticker], date(2026, 4, 22), "caution")
        self.assertEqual(result, [])
        row = mock_upsert.call_args_list[0][0][2][0]
        self.assertEqual(row["label"], "watch")


class SignalDetectorConfigCompatibilityTests(unittest.TestCase):
    """通知上限の新旧設定キー互換を確認する。"""

    def test_legacy_max_candidates_key_is_still_supported(self):
        with patch.dict(
            "src.signal_detector._SIG",
            {"max_candidates_per_pattern": "4"},
            clear=True,
        ):
            self.assertEqual(_max_entry_sets_per_batch(), 4)

    def test_new_max_entry_sets_key_has_priority(self):
        with patch.dict(
            "src.signal_detector._SIG",
            {
                "max_entry_sets_per_batch": "5",
                "max_candidates_per_pattern": "2",
            },
            clear=True,
        ):
            self.assertEqual(_max_entry_sets_per_batch(), 5)


class SignalSaveHealthTests(unittest.TestCase):
    """daily_signals 保存件数の異常検知テスト。"""

    def _mock_conn(self):
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=MagicMock())
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    @patch("src.signal_detector.is_breakout_enabled", return_value=True)
    @patch("src.signal_detector.fetch_one", return_value={"c": 1})
    @patch("src.signal_detector.get_connection")
    def test_mismatch_returns_alert(self, mock_conn, _mock_fetch, _mock_breakout):
        mock_conn.return_value = self._mock_conn()

        alert = diagnose_signal_save_count(date(2026, 6, 1), "JP", 1)

        self.assertIsNotNone(alert)
        self.assertIn("期待=2", alert)
        self.assertIn("実績=1", alert)

    @patch("src.signal_detector.is_breakout_enabled", return_value=True)
    @patch("src.signal_detector.fetch_one", return_value={"c": 2})
    @patch("src.signal_detector.get_connection")
    def test_matching_count_dual_strategy(self, mock_conn, _mock_fetch, _mock_breakout):
        mock_conn.return_value = self._mock_conn()

        alert = diagnose_signal_save_count(date(2026, 6, 1), "JP", 1)

        self.assertIsNone(alert)

    @patch("src.signal_detector.is_breakout_enabled", return_value=False)
    @patch("src.signal_detector.fetch_one", return_value={"c": 3})
    @patch("src.signal_detector.get_connection")
    def test_matching_count_returns_none(self, mock_conn, _mock_fetch, _mock_breakout):
        mock_conn.return_value = self._mock_conn()

        alert = diagnose_signal_save_count(date(2026, 6, 1), "JP", 3)

        self.assertIsNone(alert)


class DailySignalsPruneTests(unittest.TestCase):
    """スクリーニング対象外の旧 daily_signals 行が削除されること。"""

    def setUp(self):
        import sqlite3
        from contextlib import contextmanager

        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE daily_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                date TEXT NOT NULL,
                market TEXT NOT NULL,
                strategy TEXT,
                pattern TEXT,
                label TEXT NOT NULL,
                cond_c_weekly_uptrend INTEGER,
                cond_c_at_support INTEGER,
                cond_c_rsi_oversold INTEGER,
                cond_c_sar_reversal INTEGER,
                macd_confirm_type TEXT,
                close REAL,
                rsi14 REAL,
                atr14 REAL,
                vol_ratio REAL,
                rs_rank INTEGER,
                rs_score REAL,
                market_mode TEXT,
                UNIQUE(ticker, date, strategy)
            )
            """
        )
        self.conn.executemany(
            "INSERT INTO daily_signals (ticker, date, market, label) VALUES (?, ?, ?, ?)",
            [
                ("OLD1", "2026-06-22", "JP", "skip"),
                ("OLD2", "2026-06-22", "JP", "skip"),
                ("US1", "2026-06-22", "US", "skip"),
            ],
        )
        self.conn.commit()

        @contextmanager
        def _cm():
            yield self.conn
            self.conn.commit()

        self._fake_conn = _cm

    def tearDown(self):
        self.conn.close()

    def _ticker(self):
        return {
            "ticker": "7203", "market": "JP", "name": "トヨタ",
            "close": 3000.0,
            "ema25": 3100.0, "ema75": 2900.0,
            "rsi14": 32.0, "atr14": 80.0,
            "volume": 1000000, "vol_20d_avg": 700000,
            "support_level": 2850.0,
            "macd_diff": 10.0, "macd_dea": 5.0, "macd_hist": 5.0,
            "macd_diff_prev": -2.0, "macd_dea_prev": 4.0, "macd_hist_prev": -1.0,
            "est_turnover": 3000000000, "est_market_cap": 50000000000,
        }

    @patch("src.signal_detector.is_breakout_enabled", return_value=False)
    @patch("src.signal_detector.fetch_all", return_value=[])
    @patch("src.signal_detector.get_connection")
    def test_stale_rows_removed_on_save(self, mock_conn, _mock_fetch, _mock_breakout):
        mock_conn.side_effect = self._fake_conn
        detect_and_save_signals([self._ticker()], date(2026, 6, 22), "normal")

        jp_count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM daily_signals WHERE date='2026-06-22' AND market='JP'"
        ).fetchone()["c"]
        us_count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM daily_signals WHERE date='2026-06-22' AND market='US'"
        ).fetchone()["c"]
        self.assertEqual(jp_count, 1)
        self.assertEqual(us_count, 1)
        with patch("src.signal_detector.get_connection", self._fake_conn):
            self.assertIsNone(
                diagnose_signal_save_count(date(2026, 6, 22), "JP", 1),
            )


if __name__ == "__main__":
    unittest.main()
