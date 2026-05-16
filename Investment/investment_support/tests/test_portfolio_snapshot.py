"""
portfolio_snapshot モジュールのテスト

テスト対象:
- compute_and_save_snapshot(): スナップショット計算・保存
- get_latest_snapshot(): 最新スナップショット取得
- 冪等性: 同日同バッチで2回実行しても重複しない
- 保有銘柄なし: total_assets = capital のみ
"""

import sqlite3
import unittest
from contextlib import contextmanager
from datetime import date
from unittest.mock import patch


_CONFIG_MOCK = {
    "capital": {
        "total_capital_jpy": "200000",
        "total_capital_usd": "1500",
        "usdjpy_fallback": "150.0",
    },
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
}

_test_conn = None


def _setup_test_db():
    global _test_conn
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE portfolio_snapshot (
            id                        INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date             DATE    NOT NULL,
            batch_type                TEXT    NOT NULL,
            capital_jpy               REAL    NOT NULL,
            capital_usd               REAL    NOT NULL,
            holdings_value_jpy        REAL    NOT NULL DEFAULT 0,
            holdings_value_usd        REAL    NOT NULL DEFAULT 0,
            unrealized_pnl_jpy        REAL    NOT NULL DEFAULT 0,
            unrealized_pnl_usd        REAL    NOT NULL DEFAULT 0,
            total_assets_jpy          REAL    NOT NULL DEFAULT 0,
            total_assets_usd          REAL    NOT NULL DEFAULT 0,
            total_assets_combined_jpy REAL    NOT NULL DEFAULT 0,
            cash_jpy                  REAL    NOT NULL DEFAULT 0,
            cash_usd                  REAL    NOT NULL DEFAULT 0,
            usdjpy_rate               REAL    NOT NULL,
            holdings_count            INTEGER NOT NULL DEFAULT 0,
            paypay_points_value_jpy   REAL    NOT NULL DEFAULT 0,
            created_at                DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX idx_ps_date_batch
            ON portfolio_snapshot(snapshot_date, batch_type)
    """)
    conn.execute("""
        CREATE TABLE holdings (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT NOT NULL,
            name            TEXT,
            market          TEXT NOT NULL DEFAULT 'JP',
            entry_price     REAL NOT NULL,
            entry_date      DATE,
            quantity        REAL NOT NULL DEFAULT 1,
            stop_loss_price REAL,
            memo            TEXT,
            is_closed       INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE price_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker      TEXT NOT NULL,
            date        DATE NOT NULL,
            open        REAL,
            high        REAL,
            low         REAL,
            close       REAL,
            adj_close   REAL,
            volume      REAL,
            is_holiday  INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    _test_conn = conn
    return conn


@contextmanager
def _fake_get_connection():
    yield _test_conn


def _fake_fetch_all(conn, sql, params=None):
    cursor = conn.execute(sql, params or ())
    return [dict(r) for r in cursor.fetchall()]


def _fake_fetch_one(conn, sql, params=None):
    cursor = conn.execute(sql, params or ())
    row = cursor.fetchone()
    return dict(row) if row else None


def _fake_usdjpy():
    return 150.0


_NO_CASH = {"cash_jpy": 0.0, "cash_usd": 0.0}

with patch("src.config.CONFIG", _CONFIG_MOCK), \
     patch("src.data_store.get_connection", _fake_get_connection), \
     patch("src.data_store.fetch_all", _fake_fetch_all), \
     patch("src.data_store.fetch_one", _fake_fetch_one):
    from src.portfolio_snapshot import compute_and_save_snapshot, get_latest_snapshot


class TestComputeAndSaveSnapshot(unittest.TestCase):
    """compute_and_save_snapshot のテスト"""

    def setUp(self):
        _setup_test_db()

    def tearDown(self):
        global _test_conn
        if _test_conn:
            _test_conn.close()
            _test_conn = None

    @patch("src.portfolio_snapshot._fetch_moomoo_cash", return_value=_NO_CASH)
    @patch("src.portfolio_snapshot._get_usdjpy_rate", return_value=150.0)
    def test_snapshot_with_jp_and_us_holdings(self, _mock_fx, _mock_cash):
        """JP + US 混在の保有銘柄でスナップショットが正しく計算される"""
        conn = _test_conn
        # JP 銘柄: entry=1000, current=1100, qty=10 → pnl = (1100-1000)*10 = 1000
        conn.execute("INSERT INTO holdings (ticker, name, market, entry_price, quantity, is_closed) VALUES (?, ?, ?, ?, ?, ?)",
                      ("7203", "トヨタ", "JP", 1000, 10, 0))
        conn.execute("INSERT INTO price_history (ticker, date, adj_close, is_holiday) VALUES (?, ?, ?, ?)",
                      ("7203", "2026-05-15", 1100, 0))
        # US 銘柄: entry=100, current=120, qty=5 → pnl = (120-100)*5 = 100 USD
        conn.execute("INSERT INTO holdings (ticker, name, market, entry_price, quantity, is_closed) VALUES (?, ?, ?, ?, ?, ?)",
                      ("AAPL", "Apple", "US", 100, 5, 0))
        conn.execute("INSERT INTO price_history (ticker, date, adj_close, is_holiday) VALUES (?, ?, ?, ?)",
                      ("AAPL", "2026-05-15", 120, 0))
        conn.commit()

        result = compute_and_save_snapshot("evening_jp", date(2026, 5, 15))

        self.assertEqual(result["holdings_count"], 2)
        # JP: holdings_value = 1100*10 = 11000, pnl = 1000
        self.assertAlmostEqual(result["holdings_value_jpy"], 11000.0)
        self.assertAlmostEqual(result["unrealized_pnl_jpy"], 1000.0)
        self.assertAlmostEqual(result["total_assets_jpy"], 11000.0)  # = holdings_value
        # US: holdings_value = 120*5 = 600, pnl = 100
        self.assertAlmostEqual(result["holdings_value_usd"], 600.0)
        self.assertAlmostEqual(result["unrealized_pnl_usd"], 100.0)
        self.assertAlmostEqual(result["total_assets_usd"], 600.0)  # = holdings_value
        # 合計: JP holdings_value + US holdings_value * 150
        expected_combined = 11000.0 + 600.0 * 150
        self.assertAlmostEqual(result["total_assets_combined_jpy"], expected_combined)

    @patch("src.portfolio_snapshot._fetch_moomoo_cash", return_value=_NO_CASH)
    @patch("src.portfolio_snapshot._get_usdjpy_rate", return_value=150.0)
    def test_snapshot_no_holdings(self, _mock_fx, _mock_cash):
        """保有銘柄なしの場合は total_assets = 0"""
        result = compute_and_save_snapshot("morning_all", date(2026, 5, 15))

        self.assertEqual(result["holdings_count"], 0)
        self.assertAlmostEqual(result["total_assets_jpy"], 0.0)
        self.assertAlmostEqual(result["total_assets_usd"], 0.0)
        self.assertAlmostEqual(result["total_assets_combined_jpy"], 0.0)

    @patch("src.portfolio_snapshot._fetch_moomoo_cash", return_value=_NO_CASH)
    @patch("src.portfolio_snapshot._get_usdjpy_rate", return_value=150.0)
    def test_idempotent_same_day_same_batch(self, _mock_fx, _mock_cash):
        """同日同バッチで2回実行しても行数が増えない"""
        compute_and_save_snapshot("evening_jp", date(2026, 5, 15))
        compute_and_save_snapshot("evening_jp", date(2026, 5, 15))

        cursor = _test_conn.execute("SELECT COUNT(*) AS cnt FROM portfolio_snapshot")
        row = cursor.fetchone()
        self.assertEqual(row[0], 1)

    @patch("src.portfolio_snapshot._fetch_moomoo_cash", return_value=_NO_CASH)
    @patch("src.portfolio_snapshot._get_usdjpy_rate", return_value=150.0)
    def test_different_batch_types_coexist(self, _mock_fx, _mock_cash):
        """同日でもバッチタイプが違えば別行として保存される"""
        compute_and_save_snapshot("evening_jp", date(2026, 5, 15))
        compute_and_save_snapshot("morning_all", date(2026, 5, 15))

        cursor = _test_conn.execute("SELECT COUNT(*) AS cnt FROM portfolio_snapshot")
        row = cursor.fetchone()
        self.assertEqual(row[0], 2)

    @patch("src.portfolio_snapshot._fetch_moomoo_cash", return_value=_NO_CASH)
    @patch("src.portfolio_snapshot._get_usdjpy_rate", return_value=150.0)
    def test_missing_price_excluded(self, _mock_fx, _mock_cash):
        """価格データがない銘柄はスナップショットから除外される"""
        conn = _test_conn
        conn.execute("INSERT INTO holdings (ticker, name, market, entry_price, quantity, is_closed) VALUES (?, ?, ?, ?, ?, ?)",
                      ("9999", "不明銘柄", "JP", 500, 1, 0))
        conn.commit()

        result = compute_and_save_snapshot("evening_jp", date(2026, 5, 15))

        self.assertEqual(result["holdings_count"], 0)
        self.assertAlmostEqual(result["unrealized_pnl_jpy"], 0.0)

    @patch("src.portfolio_snapshot._fetch_moomoo_cash", return_value={"cash_jpy": 30000.0, "cash_usd": 500.0})
    @patch("src.portfolio_snapshot._get_usdjpy_rate", return_value=150.0)
    def test_snapshot_includes_moomoo_cash(self, _mock_fx, _mock_cash):
        """moomoo の口座現金が総資産に加算される"""
        conn = _test_conn
        # JP 銘柄: holdings_value = 1100*10 = 11000
        conn.execute("INSERT INTO holdings (ticker, name, market, entry_price, quantity, is_closed) VALUES (?, ?, ?, ?, ?, ?)",
                      ("7203", "トヨタ", "JP", 1000, 10, 0))
        conn.execute("INSERT INTO price_history (ticker, date, adj_close, is_holiday) VALUES (?, ?, ?, ?)",
                      ("7203", "2026-05-15", 1100, 0))
        conn.commit()

        result = compute_and_save_snapshot("evening_jp", date(2026, 5, 15))

        # total_assets_jpy = holdings_value(11000) + cash_jpy(30000) = 41000
        self.assertAlmostEqual(result["cash_jpy"], 30000.0)
        self.assertAlmostEqual(result["cash_usd"], 500.0)
        self.assertAlmostEqual(result["total_assets_jpy"], 11000.0 + 30000.0)
        self.assertAlmostEqual(result["total_assets_usd"], 500.0)
        # combined = 41000 + 500 * 150 = 116000
        self.assertAlmostEqual(result["total_assets_combined_jpy"], 41000.0 + 500.0 * 150)


class TestGetLatestSnapshot(unittest.TestCase):
    """get_latest_snapshot のテスト"""

    def setUp(self):
        _setup_test_db()

    def tearDown(self):
        global _test_conn
        if _test_conn:
            _test_conn.close()
            _test_conn = None

    def test_no_snapshots_returns_none(self):
        """スナップショットが存在しない場合は None を返す"""
        result = get_latest_snapshot()
        self.assertIsNone(result)

    @patch("src.portfolio_snapshot._fetch_moomoo_cash", return_value=_NO_CASH)
    @patch("src.portfolio_snapshot._get_usdjpy_rate", return_value=150.0)
    def test_returns_most_recent(self, _mock_fx, _mock_cash):
        """最新日付のスナップショットが返される"""
        compute_and_save_snapshot("evening_jp", date(2026, 5, 14))
        compute_and_save_snapshot("evening_jp", date(2026, 5, 15))

        result = get_latest_snapshot()
        self.assertIsNotNone(result)
        self.assertEqual(result["snapshot_date"], "2026-05-15")


if __name__ == "__main__":
    unittest.main()
