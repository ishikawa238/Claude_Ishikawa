"""
利確ターゲット管理モジュールのテスト

テスト対象:
- calc_targets(): タイプI/II のターゲット価格・売却株数
- _split_quantity(): 少数株ルール（1〜3株）
- determine_profit_type(): パターンA/B/なし → タイプI/II/III
- ensure_targets(): 自動生成・重複防止
- check_target_reached(): 到達判定とDB更新
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

# CONFIG モック
_CONFIG_MOCK = {
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
    "logging": {"log_dir": "/tmp", "log_level": "INFO"},
}

import src.config as _src_config
_src_config.CONFIG = _CONFIG_MOCK

from src.take_profit import (
    _split_quantity,
    calc_targets,
    check_target_reached,
    determine_profit_type,
    ensure_targets,
    get_targets,
)

# テスト用 DB 接続
_test_conn = None


@contextmanager
def _get_test_connection():
    yield _test_conn


def _init_test_db(conn):
    """テスト用テーブルを作成する。"""
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_signal_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_date DATE NOT NULL,
            ticker TEXT NOT NULL,
            name TEXT,
            market TEXT NOT NULL,
            pattern TEXT NOT NULL,
            entry_price REAL NOT NULL,
            stop_loss_price REAL NOT NULL,
            stop_loss_pct REAL NOT NULL,
            target_price REAL NOT NULL,
            recommended_amount REAL,
            market_mode TEXT NOT NULL,
            conditions_met TEXT,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS take_profit_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            profit_type TEXT NOT NULL,
            r_value REAL,
            t1_price REAL, t1_pct REAL,
            t1_reached INTEGER NOT NULL DEFAULT 0, t1_reached_date DATE,
            t2_price REAL, t2_pct REAL,
            t2_reached INTEGER NOT NULL DEFAULT 0, t2_reached_date DATE,
            t3_price REAL, t3_pct REAL,
            t3_reached INTEGER NOT NULL DEFAULT 0, t3_reached_date DATE,
            adjusted_stop REAL,
            trailing_active INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS holdings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            name TEXT, market TEXT NOT NULL,
            entry_price REAL NOT NULL, entry_date DATE NOT NULL,
            quantity REAL, stop_loss_price REAL, memo TEXT,
            is_closed INTEGER NOT NULL DEFAULT 0,
            close_price REAL, close_date DATE, close_reason TEXT,
            created_at DATETIME, updated_at DATETIME
        )
    """)
    conn.commit()


class TestCalcTargets(unittest.TestCase):
    """calc_targets() のテスト"""

    def test_type_i_standard(self):
        """タイプI: 標準的なターゲット計算（7株）"""
        result = calc_targets(
            entry_price=1000.0,
            stop_loss_price=930.0,
            profit_type="I",
            quantity=7,
        )
        self.assertEqual(result["profit_type"], "I")
        self.assertAlmostEqual(result["r_value"], 70.0)
        self.assertAlmostEqual(result["t1_price"], 1035.0)   # +0.5R
        self.assertAlmostEqual(result["t2_price"], 1070.0)   # +1.0R
        self.assertAlmostEqual(result["t3_price"], 1140.0)   # +2.0R
        self.assertEqual(result["t1_pct"], 50)
        self.assertEqual(result["t2_pct"], 30)
        self.assertEqual(result["t3_pct"], 20)
        # 7株: T1=4, T2=2, T3=1
        self.assertEqual(result["t1_qty"], 4)
        self.assertEqual(result["t2_qty"], 2)
        self.assertEqual(result["t3_qty"], 1)

    def test_type_ii_standard(self):
        """タイプII: 標準的なターゲット計算（10株）"""
        result = calc_targets(
            entry_price=2500.0,
            stop_loss_price=2360.0,
            profit_type="II",
            quantity=10,
        )
        self.assertEqual(result["profit_type"], "II")
        self.assertAlmostEqual(result["r_value"], 140.0)
        self.assertAlmostEqual(result["t1_price"], 2640.0)   # +1.0R
        self.assertAlmostEqual(result["t2_price"], 2780.0)   # +2.0R
        self.assertIsNone(result["t3_price"])                 # MA25トレーリング
        self.assertEqual(result["t1_pct"], 30)
        self.assertEqual(result["t2_pct"], 30)
        self.assertEqual(result["t3_pct"], 40)

    def test_type_iii_returns_empty(self):
        """タイプIII: 空辞書を返す"""
        result = calc_targets(1000.0, 930.0, "III", 10)
        self.assertEqual(result, {})

    def test_zero_r_returns_empty(self):
        """R <= 0 の場合は空辞書"""
        result = calc_targets(1000.0, 1000.0, "I", 10)
        self.assertEqual(result, {})

    def test_negative_r_returns_empty(self):
        """entry < stop_loss の場合は空辞書"""
        result = calc_targets(900.0, 1000.0, "I", 10)
        self.assertEqual(result, {})

    def test_no_quantity(self):
        """quantity=None の場合は1株として扱う"""
        result = calc_targets(1000.0, 930.0, "I", None)
        self.assertEqual(result["t1_qty"], 1)
        self.assertEqual(result["t2_qty"], 0)
        self.assertEqual(result["t3_qty"], 0)


class TestSplitQuantity(unittest.TestCase):
    """_split_quantity() の少数株ルールテスト"""

    def test_1_share(self):
        """1株: T1で全売却"""
        self.assertEqual(_split_quantity(1, 50, 30, 20), (1, 0, 0))

    def test_2_shares(self):
        """2株: T1で1株、残り1株"""
        self.assertEqual(_split_quantity(2, 50, 30, 20), (1, 0, 1))

    def test_3_shares(self):
        """3株: T1で2株、残り1株"""
        self.assertEqual(_split_quantity(3, 50, 30, 20), (2, 0, 1))

    def test_7_shares_type_i(self):
        """7株 タイプI (50/30/20): T1=4, T2=2, T3=1"""
        t1, t2, t3 = _split_quantity(7, 50, 30, 20)
        self.assertEqual(t1 + t2 + t3, 7)
        self.assertEqual(t3, 1)
        self.assertEqual(t2, 2)
        self.assertEqual(t1, 4)

    def test_10_shares_type_i(self):
        """10株 タイプI (50/30/20): T1=5, T2=3, T3=2"""
        t1, t2, t3 = _split_quantity(10, 50, 30, 20)
        self.assertEqual(t1 + t2 + t3, 10)
        self.assertEqual(t3, 2)
        self.assertEqual(t2, 3)
        self.assertEqual(t1, 5)

    def test_10_shares_type_ii(self):
        """10株 タイプII (30/30/40): T1=3, T2=3, T3=4"""
        t1, t2, t3 = _split_quantity(10, 30, 30, 40)
        self.assertEqual(t1 + t2 + t3, 10)
        self.assertEqual(t3, 4)
        self.assertEqual(t2, 3)
        self.assertEqual(t1, 3)

    def test_0_shares(self):
        """0株: すべて0"""
        self.assertEqual(_split_quantity(0, 50, 30, 20), (0, 0, 0))

    def test_total_preserved(self):
        """合計株数は常に元の保有数と一致する"""
        for total in range(1, 20):
            for pcts in [(50, 30, 20), (30, 30, 40)]:
                t1, t2, t3 = _split_quantity(total, *pcts)
                self.assertEqual(
                    t1 + t2 + t3, total,
                    f"total={total}, pcts={pcts}: {t1}+{t2}+{t3}={t1+t2+t3}",
                )


class TestDetermineProfitType(unittest.TestCase):
    """determine_profit_type() のテスト"""

    def setUp(self):
        global _test_conn
        _test_conn = sqlite3.connect(":memory:")
        _test_conn.row_factory = sqlite3.Row
        _init_test_db(_test_conn)
        self._patcher = patch(
            "src.take_profit.get_connection", side_effect=_get_test_connection
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        _test_conn.close()

    def test_pattern_a_returns_type_i(self):
        _test_conn.execute(
            "INSERT INTO trade_signal_log "
            "(signal_date, ticker, market, pattern, entry_price, stop_loss_price, "
            "stop_loss_pct, target_price, market_mode) "
            "VALUES ('2026-01-01', 'AAPL', 'US', 'A', 185.5, 170.0, 8.0, 210.0, 'normal')"
        )
        _test_conn.commit()
        self.assertEqual(determine_profit_type("AAPL"), "I")

    def test_pattern_b_returns_type_ii(self):
        _test_conn.execute(
            "INSERT INTO trade_signal_log "
            "(signal_date, ticker, market, pattern, entry_price, stop_loss_price, "
            "stop_loss_pct, target_price, market_mode) "
            "VALUES ('2026-01-01', '7203', 'JP', 'B', 3200, 2944, 8.0, 3712, 'normal')"
        )
        _test_conn.commit()
        self.assertEqual(determine_profit_type("7203"), "II")

    def test_no_signal_returns_type_iii(self):
        self.assertEqual(determine_profit_type("UNKNOWN"), "III")


class TestEnsureAndCheckTargets(unittest.TestCase):
    """ensure_targets() と check_target_reached() の統合テスト"""

    def setUp(self):
        global _test_conn
        _test_conn = sqlite3.connect(":memory:")
        _test_conn.row_factory = sqlite3.Row
        _init_test_db(_test_conn)
        self._patcher = patch(
            "src.take_profit.get_connection", side_effect=_get_test_connection
        )
        self._patcher.start()

        # パターンA のシグナルを登録
        _test_conn.execute(
            "INSERT INTO trade_signal_log "
            "(signal_date, ticker, market, pattern, entry_price, stop_loss_price, "
            "stop_loss_pct, target_price, market_mode) "
            "VALUES ('2026-01-01', 'TEST', 'JP', 'A', 1000, 930, 7.0, 1140, 'normal')"
        )
        # 保有銘柄を登録
        _test_conn.execute(
            "INSERT INTO holdings "
            "(ticker, name, market, entry_price, entry_date, quantity, "
            "stop_loss_price, is_closed, created_at, updated_at) "
            "VALUES ('TEST', 'テスト', 'JP', 1000, '2026-01-01', 7, 930, 0, "
            "datetime('now'), datetime('now'))"
        )
        _test_conn.commit()

    def tearDown(self):
        self._patcher.stop()
        _test_conn.close()

    def test_ensure_creates_targets(self):
        """ensure_targets で行が作成される"""
        result = ensure_targets("TEST", 1000.0, 930.0, 7, "2026-01-02")
        self.assertIsNotNone(result)
        self.assertEqual(result["profit_type"], "I")
        self.assertAlmostEqual(result["t1_price"], 1035.0)
        self.assertAlmostEqual(result["t2_price"], 1070.0)
        self.assertAlmostEqual(result["t3_price"], 1140.0)
        self.assertEqual(result["t1_reached"], 0)

    def test_ensure_idempotent(self):
        """ensure_targets は2回呼んでも1行しか作られない"""
        ensure_targets("TEST", 1000.0, 930.0, 7, "2026-01-02")
        ensure_targets("TEST", 1000.0, 930.0, 7, "2026-01-03")
        rows = _test_conn.execute(
            "SELECT COUNT(*) as cnt FROM take_profit_targets WHERE ticker = 'TEST'"
        ).fetchone()
        self.assertEqual(rows["cnt"], 1)

    def test_check_t1_reached(self):
        """T1 到達判定"""
        ensure_targets("TEST", 1000.0, 930.0, 7, "2026-01-02")
        result = check_target_reached("TEST", 1040.0, 1000.0, "2026-01-05")
        self.assertIn("t1", result["newly_reached"])
        self.assertAlmostEqual(result["adjusted_stop"], 1000.0)
        self.assertGreater(result["sell_qty"], 0)

    def test_check_t1_not_reached(self):
        """T1 未到達"""
        ensure_targets("TEST", 1000.0, 930.0, 7, "2026-01-02")
        result = check_target_reached("TEST", 1020.0, 1000.0, "2026-01-05")
        self.assertEqual(result["newly_reached"], [])

    def test_check_t1_reached_only_once(self):
        """T1 到達は1回のみ記録される"""
        ensure_targets("TEST", 1000.0, 930.0, 7, "2026-01-02")
        check_target_reached("TEST", 1040.0, 1000.0, "2026-01-05")
        result = check_target_reached("TEST", 1050.0, 1000.0, "2026-01-06")
        self.assertNotIn("t1", result["newly_reached"])

    def test_check_multiple_targets_at_once(self):
        """一度に複数のターゲットに到達"""
        ensure_targets("TEST", 1000.0, 930.0, 7, "2026-01-02")
        result = check_target_reached("TEST", 1150.0, 1000.0, "2026-01-05")
        self.assertIn("t1", result["newly_reached"])
        self.assertIn("t2", result["newly_reached"])
        self.assertIn("t3", result["newly_reached"])

    def test_type_iii_no_targets(self):
        """タイプIII: ensure_targets は None を返す"""
        result = ensure_targets("UNKNOWN", 1000.0, 930.0, 7, "2026-01-02")
        self.assertIsNone(result)


class TestTrailingStop(unittest.TestCase):
    """タイプII のトレーリングストップのテスト"""

    def setUp(self):
        global _test_conn
        _test_conn = sqlite3.connect(":memory:")
        _test_conn.row_factory = sqlite3.Row
        _init_test_db(_test_conn)
        self._patcher = patch(
            "src.take_profit.get_connection", side_effect=_get_test_connection
        )
        self._patcher.start()

        # パターンB のシグナルを登録
        _test_conn.execute(
            "INSERT INTO trade_signal_log "
            "(signal_date, ticker, market, pattern, entry_price, stop_loss_price, "
            "stop_loss_pct, target_price, market_mode) "
            "VALUES ('2026-01-01', 'TREND', 'JP', 'B', 2500, 2360, 5.6, 2780, 'normal')"
        )
        _test_conn.execute(
            "INSERT INTO holdings "
            "(ticker, name, market, entry_price, entry_date, quantity, "
            "stop_loss_price, is_closed, created_at, updated_at) "
            "VALUES ('TREND', 'トレンド', 'JP', 2500, '2026-01-01', 10, 2360, 0, "
            "datetime('now'), datetime('now'))"
        )
        _test_conn.commit()

    def tearDown(self):
        self._patcher.stop()
        _test_conn.close()

    def test_trailing_not_active_before_t2(self):
        """T2未到達ではトレーリングは発動しない"""
        ensure_targets("TREND", 2500.0, 2360.0, 10, "2026-01-02")
        result = check_target_reached("TREND", 2640.0, 2500.0, "2026-01-05", ma25=2650.0)
        targets = get_targets("TREND")
        self.assertEqual(targets["trailing_active"], 0)

    def test_trailing_active_after_t2(self):
        """T2到達後にトレーリングが有効化される"""
        ensure_targets("TREND", 2500.0, 2360.0, 10, "2026-01-02")
        # T1+T2 に同時到達
        check_target_reached("TREND", 2800.0, 2500.0, "2026-01-05")
        targets = get_targets("TREND")
        self.assertEqual(targets["trailing_active"], 1)

    def test_trailing_triggers_on_ma25_break(self):
        """T2到達後にMA25割れでトレーリング発動"""
        ensure_targets("TREND", 2500.0, 2360.0, 10, "2026-01-02")
        check_target_reached("TREND", 2800.0, 2500.0, "2026-01-05")
        result = check_target_reached("TREND", 2600.0, 2500.0, "2026-01-10", ma25=2620.0)
        self.assertIn("t3", result["newly_reached"])
        self.assertTrue(result["trailing_triggered"])


if __name__ == "__main__":
    unittest.main()
