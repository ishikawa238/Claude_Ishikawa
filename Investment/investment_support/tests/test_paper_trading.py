"""
paper_trading モジュールのテスト

テスト対象:
- record_paper_entries(): 仮想エントリー記録
- update_paper_trades(): 日次更新（損切り・利確判定）
- get_paper_trade_summary(): サマリー生成
"""

import sqlite3
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
    "paper_mode": {"enabled": True},
}

# paper_trading は data_store 経由で DB にアクセスするため、
# インメモリ DB を使ったテスト用コンテキストマネージャでモックする
_test_conn = None


def _setup_test_db():
    """テスト用インメモリ DB を作成し paper_trades テーブルを作成する。"""
    global _test_conn
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE paper_trades (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_date      DATE    NOT NULL,
            ticker           TEXT    NOT NULL,
            name             TEXT,
            market           TEXT    NOT NULL,
            pattern          TEXT,
            entry_price      REAL    NOT NULL,
            stop_loss_price  REAL    NOT NULL,
            target_price     REAL    NOT NULL,
            position_size    REAL,
            exit_price       REAL,
            exit_date        DATE,
            exit_reason      TEXT,
            pnl_pct          REAL,
            pnl_amount       REAL,
            status           TEXT    NOT NULL DEFAULT 'open',
            created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
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


from contextlib import contextmanager


@contextmanager
def _fake_get_connection():
    yield _test_conn


def _fake_insert_one(conn, table, row):
    cols = [k for k in row if k != "id"]
    placeholders = ", ".join("?" for _ in cols)
    col_names = ", ".join(cols)
    vals = [row[k] for k in cols]
    cursor = conn.execute(f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})", vals)
    conn.commit()
    return cursor.lastrowid


def _fake_fetch_all(conn, sql, params=None):
    cursor = conn.execute(sql, params or ())
    return [dict(r) for r in cursor.fetchall()]


def _fake_fetch_one(conn, sql, params=None):
    cursor = conn.execute(sql, params or ())
    row = cursor.fetchone()
    return dict(row) if row else None


with patch("src.config.CONFIG", _CONFIG_MOCK), \
     patch("src.data_store.get_connection", _fake_get_connection), \
     patch("src.data_store.insert_one", _fake_insert_one), \
     patch("src.data_store.fetch_all", _fake_fetch_all), \
     patch("src.data_store.fetch_one", _fake_fetch_one):
    from src.paper_trading import (
        format_summary,
        get_paper_trade_summary,
        is_paper_mode_enabled,
        record_paper_entries,
        update_paper_trades,
    )


class ConfigPatchMixin:
    """各テストでテスト用 config を使う。"""

    def setUp(self):
        super().setUp()
        self._config_patcher = patch("src.config.CONFIG", _CONFIG_MOCK)
        self._config_patcher.start()

    def tearDown(self):
        self._config_patcher.stop()
        super().tearDown()


def _make_entry_set(**kwargs):
    """テスト用エントリーセット辞書を生成する。"""
    base = {
        "signal_date": "2026-04-30",
        "ticker": "7203.TSE",
        "name": "トヨタ自動車",
        "market": "JP",
        "pattern": "A",
        "entry_price": 2500.0,
        "stop_loss_price": 2300.0,
        "target_price": 2900.0,
        "recommended_amount": 50000.0,
        "market_mode": "normal",
    }
    base.update(kwargs)
    return base


class IsPaperModeEnabledTests(ConfigPatchMixin, unittest.TestCase):

    def test_enabled(self):
        self.assertTrue(is_paper_mode_enabled())


class RecordPaperEntriesTests(ConfigPatchMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        _setup_test_db()

    def test_record_single_entry(self):
        """1件のエントリーセットが記録される"""
        entries = [_make_entry_set()]
        result = record_paper_entries(entries)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["ticker"], "7203.TSE")
        self.assertIsNotNone(result[0]["id"])

    def test_skip_reason_not_recorded(self):
        """_skip_reason がある場合は記録しない"""
        entries = [_make_entry_set(_skip_reason="資金不足のため見送り")]
        result = record_paper_entries(entries)
        self.assertEqual(len(result), 0)

    def test_max_holdings_limit(self):
        """保有上限を超えない"""
        entries = [
            _make_entry_set(ticker="7203.TSE"),
            _make_entry_set(ticker="9984.TSE"),
            _make_entry_set(ticker="6758.TSE"),
            _make_entry_set(ticker="8035.TSE"),  # 4件目 → 上限3で拒否
        ]
        result = record_paper_entries(entries)
        self.assertEqual(len(result), 3)

    def test_duplicate_ticker_skipped(self):
        """同銘柄の重複は記録しない"""
        entries = [
            _make_entry_set(ticker="7203.TSE"),
            _make_entry_set(ticker="7203.TSE"),  # 重複
        ]
        result = record_paper_entries(entries)
        self.assertEqual(len(result), 1)


class UpdatePaperTradesTests(ConfigPatchMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        _setup_test_db()

    def _insert_trade(self, ticker="7203.TSE", entry_price=2500.0,
                      stop_loss=2300.0, target=2900.0):
        _test_conn.execute(
            "INSERT INTO paper_trades "
            "(signal_date, ticker, name, market, pattern, entry_price, "
            " stop_loss_price, target_price, position_size, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-04-28", ticker, "テスト", "JP", "A",
             entry_price, stop_loss, target, 50000, "open"),
        )
        _test_conn.commit()

    def _insert_price(self, ticker, date_str, high, low, close):
        _test_conn.execute(
            "INSERT INTO price_history "
            "(ticker, date, open, high, low, close, adj_close, volume, is_holiday) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ticker, date_str, close, high, low, close, close, 100000, 0),
        )
        _test_conn.commit()

    def test_stop_loss_triggered(self):
        """安値が損切りラインを下回ったらクローズ"""
        self._insert_trade()
        self._insert_price("7203.TSE", "2026-04-30", high=2500, low=2200, close=2250)

        from datetime import date
        result = update_paper_trades(date(2026, 4, 30))
        self.assertEqual(len(result["closed"]), 1)
        self.assertEqual(result["closed"][0]["exit_reason"], "stop_loss")
        self.assertEqual(result["closed"][0]["pnl_amount"], -4000.0)
        self.assertEqual(len(result["open"]), 0)

    def test_take_profit_triggered(self):
        """高値が利確ラインに達したらクローズ"""
        self._insert_trade()
        self._insert_price("7203.TSE", "2026-04-30", high=3000, low=2600, close=2950)

        from datetime import date
        result = update_paper_trades(date(2026, 4, 30))
        self.assertEqual(len(result["closed"]), 1)
        self.assertEqual(result["closed"][0]["exit_reason"], "take_profit")
        self.assertEqual(result["closed"][0]["pnl_amount"], 8000.0)

    def test_no_trigger(self):
        """SL/TP に達しなければオープンのまま"""
        self._insert_trade()
        self._insert_price("7203.TSE", "2026-04-30", high=2600, low=2400, close=2550)

        from datetime import date
        result = update_paper_trades(date(2026, 4, 30))
        self.assertEqual(len(result["closed"]), 0)
        self.assertEqual(len(result["open"]), 1)

    def test_no_open_trades(self):
        """オープンポジションがない場合"""
        from datetime import date
        result = update_paper_trades(date(2026, 4, 30))
        self.assertEqual(len(result["closed"]), 0)
        self.assertEqual(len(result["open"]), 0)


class GetPaperTradeSummaryTests(ConfigPatchMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        _setup_test_db()

    def test_empty_summary(self):
        """トレードがない場合"""
        summary = get_paper_trade_summary()
        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["closed"], 0)
        self.assertEqual(summary["win_rate"], 0.0)

    def test_summary_with_closed_trades(self):
        """クローズ済みトレードがある場合"""
        _test_conn.execute(
            "INSERT INTO paper_trades "
            "(signal_date, ticker, name, market, pattern, entry_price, "
            " stop_loss_price, target_price, position_size, status, "
            " exit_price, exit_date, exit_reason, pnl_pct, pnl_amount) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-04-25", "7203.TSE", "テスト", "JP", "A",
             2500, 2300, 2900, 50000, "closed",
             2900, "2026-04-28", "take_profit", 16.0, 8000),
        )
        _test_conn.execute(
            "INSERT INTO paper_trades "
            "(signal_date, ticker, name, market, pattern, entry_price, "
            " stop_loss_price, target_price, position_size, status, "
            " exit_price, exit_date, exit_reason, pnl_pct, pnl_amount) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-04-26", "9984.TSE", "テスト2", "JP", "B",
             5000, 4600, 5800, 40000, "closed",
             4600, "2026-04-29", "stop_loss", -8.0, -3200),
        )
        _test_conn.commit()

        summary = get_paper_trade_summary()
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["closed"], 2)
        self.assertEqual(summary["wins"], 1)
        self.assertEqual(summary["losses"], 1)
        self.assertEqual(summary["win_rate"], 50.0)

    def test_format_summary_output(self):
        """format_summary がエラーなく文字列を返す"""
        summary = get_paper_trade_summary()
        output = format_summary(summary)
        self.assertIn("ペーパートレード", output)


if __name__ == "__main__":
    unittest.main()
