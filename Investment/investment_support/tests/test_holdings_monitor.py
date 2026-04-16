"""
holdings_monitor モジュールのテスト

テスト対象:
- sync_holdings_from_csv(): 冪等性（複数回呼んでも重複しない）
- _dedup_open_holdings(): 既存重複行のクリーンアップ
- _upsert_holdings(): UPDATE と INSERT の振り分け
- ensure_holding_prices(): price_history にないETF銘柄の価格補完
"""

import csv
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch


def _make_temp_db() -> sqlite3.Connection:
    """インメモリ SQLite DB を作成してholdings/price_historyテーブルを用意する。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("""
        CREATE TABLE holdings (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker           TEXT    NOT NULL,
            name             TEXT,
            market           TEXT    NOT NULL,
            entry_price      REAL    NOT NULL,
            entry_date       DATE,
            quantity         REAL,
            stop_loss_price  REAL,
            memo             TEXT,
            is_closed        INTEGER NOT NULL DEFAULT 0,
            close_price      REAL,
            close_date       DATE,
            close_reason     TEXT,
            created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE price_history (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker    TEXT NOT NULL,
            date      DATE NOT NULL,
            open      REAL,
            high      REAL,
            low       REAL,
            close     REAL,
            adj_close REAL,
            volume    REAL,
            is_holiday INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    return conn


class DeduplicateOpenHoldingsTests(unittest.TestCase):
    """_dedup_open_holdings: 重複行の削除テスト"""

    def test_removes_duplicate_open_rows_keeping_max_id(self):
        from src.holdings_monitor import _dedup_open_holdings

        conn = _make_temp_db()
        # 同一ティッカーで3行INSERT（重複状態を再現）
        for _ in range(3):
            conn.execute(
                "INSERT INTO holdings (ticker, market, entry_price, is_closed) "
                "VALUES ('NVDA', 'US', 180.0, 0)"
            )
        conn.commit()

        _dedup_open_holdings(conn)
        conn.commit()

        rows = conn.execute(
            "SELECT id FROM holdings WHERE ticker='NVDA' AND is_closed=0"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        # 残るのは最大id
        max_id = conn.execute(
            "SELECT MAX(id) FROM holdings WHERE ticker='NVDA' AND is_closed=0"
        ).fetchone()[0]
        self.assertEqual(rows[0]["id"], max_id)

    def test_does_not_remove_closed_rows(self):
        from src.holdings_monitor import _dedup_open_holdings

        conn = _make_temp_db()
        # is_closed=1 の行は重複対象外
        conn.execute(
            "INSERT INTO holdings (ticker, market, entry_price, is_closed) "
            "VALUES ('NVDA', 'US', 180.0, 1)"
        )
        conn.execute(
            "INSERT INTO holdings (ticker, market, entry_price, is_closed) "
            "VALUES ('NVDA', 'US', 200.0, 1)"
        )
        conn.commit()

        _dedup_open_holdings(conn)
        conn.commit()

        rows = conn.execute(
            "SELECT id FROM holdings WHERE ticker='NVDA'"
        ).fetchall()
        # 両方の closed 行が残る
        self.assertEqual(len(rows), 2)


class UpsertHoldingsTests(unittest.TestCase):
    """_upsert_holdings: UPDATE/INSERT の振り分けテスト"""

    def test_inserts_new_ticker(self):
        from src.holdings_monitor import _upsert_holdings

        conn = _make_temp_db()
        rows = [{
            "ticker": "AAPL", "name": "Apple", "market": "US",
            "entry_price": 150.0, "entry_date": "2026-01-01",
            "quantity": 1.0, "stop_loss_price": 138.0, "memo": None,
        }]
        _upsert_holdings(conn, rows)
        conn.commit()

        result = conn.execute(
            "SELECT COUNT(*) as cnt FROM holdings WHERE ticker='AAPL' AND is_closed=0"
        ).fetchone()
        self.assertEqual(result["cnt"], 1)

    def test_updates_existing_ticker_without_duplicate(self):
        from src.holdings_monitor import _upsert_holdings

        conn = _make_temp_db()
        # 既存行を1行作成
        conn.execute(
            "INSERT INTO holdings (ticker, market, entry_price, quantity, is_closed) "
            "VALUES ('NVDA', 'US', 180.0, 0.05, 0)"
        )
        conn.commit()

        # 価格・数量を変えて再度upsert
        rows = [{
            "ticker": "NVDA", "name": "NVIDIA", "market": "US",
            "entry_price": 190.0, "entry_date": "2026-04-01",
            "quantity": 0.1, "stop_loss_price": 174.8, "memo": None,
        }]
        _upsert_holdings(conn, rows)
        conn.commit()

        all_rows = conn.execute(
            "SELECT * FROM holdings WHERE ticker='NVDA' AND is_closed=0"
        ).fetchall()
        # 重複しない（1行のまま）
        self.assertEqual(len(all_rows), 1)
        # 値が更新されている
        self.assertAlmostEqual(all_rows[0]["entry_price"], 190.0)
        self.assertAlmostEqual(all_rows[0]["quantity"], 0.1)

    def test_does_not_touch_closed_rows(self):
        from src.holdings_monitor import _upsert_holdings

        conn = _make_temp_db()
        # 決済済み行
        conn.execute(
            "INSERT INTO holdings (ticker, market, entry_price, is_closed) "
            "VALUES ('GOOG', 'US', 150.0, 1)"
        )
        conn.commit()

        rows = [{
            "ticker": "GOOG", "name": "Alphabet", "market": "US",
            "entry_price": 180.0, "entry_date": "2026-04-01",
            "quantity": 0.07, "stop_loss_price": 165.6, "memo": None,
        }]
        _upsert_holdings(conn, rows)
        conn.commit()

        closed = conn.execute(
            "SELECT entry_price FROM holdings WHERE ticker='GOOG' AND is_closed=1"
        ).fetchone()
        # 決済済み行は書き換えられない
        self.assertAlmostEqual(closed["entry_price"], 150.0)

        open_rows = conn.execute(
            "SELECT COUNT(*) as cnt FROM holdings WHERE ticker='GOOG' AND is_closed=0"
        ).fetchone()
        # 新規オープン行が追加される
        self.assertEqual(open_rows["cnt"], 1)


class SyncHoldingsFromCsvIdempotencyTests(unittest.TestCase):
    """sync_holdings_from_csv: 複数回呼んでも重複しない（冪等性）テスト"""

    def _make_csv(self, tmpdir: Path) -> Path:
        csv_path = tmpdir / "holdings.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["ticker", "name", "market", "entry_price",
                            "entry_date", "quantity", "stop_loss_price", "memo"],
            )
            writer.writeheader()
            writer.writerow({
                "ticker": "VT", "name": "Vanguard Total World",
                "market": "US", "entry_price": "139.655",
                "entry_date": "2026-04-11", "quantity": "0.116",
                "stop_loss_price": "128.48", "memo": "",
            })
            writer.writerow({
                "ticker": "NVDA", "name": "NVIDIA",
                "market": "US", "entry_price": "180.254",
                "entry_date": "2026-04-11", "quantity": "0.0552",
                "stop_loss_price": "159.51", "memo": "",
            })
        return csv_path

    def test_calling_twice_does_not_duplicate_rows(self):
        import src.holdings_monitor as hm

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = self._make_csv(Path(tmpdir))

            # テスト用インメモリDBとCSVパスをパッチ
            conn = _make_temp_db()

            def fake_get_connection():
                from contextlib import contextmanager

                @contextmanager
                def _cm():
                    try:
                        yield conn
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                return _cm()

            with patch.object(hm, "_HOLDINGS_CSV", csv_path), \
                 patch("src.holdings_monitor.get_connection", fake_get_connection):
                hm.sync_holdings_from_csv()
                hm.sync_holdings_from_csv()
                hm.sync_holdings_from_csv()

            rows = conn.execute(
                "SELECT ticker, COUNT(*) as cnt FROM holdings "
                "WHERE is_closed=0 GROUP BY ticker"
            ).fetchall()
            for row in rows:
                self.assertEqual(
                    row["cnt"], 1,
                    f"{row['ticker']} が {row['cnt']} 件重複しています"
                )
            tickers = {r["ticker"] for r in rows}
            self.assertIn("VT", tickers)
            self.assertIn("NVDA", tickers)


class EnsureHoldingPricesTests(unittest.TestCase):
    """ensure_holding_prices: price_historyにないETF銘柄を補完フェッチするテスト"""

    def test_fetches_price_for_ticker_missing_from_price_history(self):
        import src.holdings_monitor as hm

        conn = _make_temp_db()
        # holdingsにVTを登録（price_historyにはデータなし）
        conn.execute(
            "INSERT INTO holdings (ticker, market, entry_price, is_closed) "
            "VALUES ('VT', 'US', 139.655, 0)"
        )
        conn.commit()

        fake_prices = [{
            "ticker": "VT", "date": "2026-04-14",
            "open": 138.0, "high": 140.0, "low": 137.5,
            "close": 139.0, "adj_close": 139.0, "volume": 500000.0,
            "is_holiday": 0,
        }]

        def fake_get_connection():
            from contextlib import contextmanager

            @contextmanager
            def _cm():
                try:
                    yield conn
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            return _cm()

        # ensure_holding_prices は fetch_price_history を関数内で
        # "from src.data_fetcher import fetch_price_history" するため
        # src.data_fetcher.fetch_price_history をパッチする
        with patch("src.holdings_monitor.get_connection", fake_get_connection), \
             patch("src.holdings_monitor.upsert") as mock_upsert, \
             patch("src.data_fetcher.fetch_price_history", return_value=fake_prices):
            fetched = hm.ensure_holding_prices(date(2026, 4, 15))

        self.assertIn("VT", fetched)

    def test_skips_ticker_with_recent_price_history(self):
        import src.holdings_monitor as hm

        conn = _make_temp_db()
        conn.execute(
            "INSERT INTO holdings (ticker, market, entry_price, is_closed) "
            "VALUES ('NVDA', 'US', 180.0, 0)"
        )
        # price_history に既存データあり
        conn.execute(
            "INSERT INTO price_history (ticker, date, adj_close, is_holiday) "
            "VALUES ('NVDA', '2026-04-14', 185.0, 0)"
        )
        conn.commit()

        def fake_get_connection():
            from contextlib import contextmanager

            @contextmanager
            def _cm():
                try:
                    yield conn
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            return _cm()

        with patch("src.holdings_monitor.get_connection", fake_get_connection):
            fetched = hm.ensure_holding_prices(date(2026, 4, 15))

        # 既存データがあるためフェッチしない
        self.assertNotIn("NVDA", fetched)


if __name__ == "__main__":
    unittest.main()
