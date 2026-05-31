"""
holdings.csv 同期のエッジケーステスト

テスト対象: src/holdings_monitor.py の sync_holdings_from_csv()

カバーするシナリオ:
1. ヘッダーのみ（空CSV）→ エラーなし、0件同期
2. 同一ティッカーが重複（2行）→ 最新の1行だけ残る（graceful handling）
3. 必須カラム（entry_price）が欠落 → ログ警告のみでクラッシュしない
4. ファイル不在 → 警告ログのみでクラッシュしない、返り値 0
"""

import csv
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

# sys.path を揃える
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

# ─── CONFIG モック（import 前に設定） ───
_CONFIG_MOCK = {
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
    "logging":  {"log_dir": "/tmp", "log_level": "INFO"},
    "capital":  {"default_stop_loss_pct": "8"},
}

import src.config as _src_config
_src_config.CONFIG = _CONFIG_MOCK

import src.holdings_monitor as hm


# ============================================================
# ヘルパー
# ============================================================

def _make_temp_db() -> sqlite3.Connection:
    """インメモリ SQLite DB に holdings テーブルを作成して返す。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("""
        CREATE TABLE holdings (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker           TEXT    NOT NULL,
            name             TEXT,
            market           TEXT    NOT NULL DEFAULT '',
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
    conn.commit()
    return conn


def _make_fake_connection(conn: sqlite3.Connection):
    """get_connection() コンテキストマネージャのファクトリを返す。"""
    def _factory():
        @contextmanager
        def _cm():
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return _cm()
    return _factory


_CSV_FIELDNAMES = ["ticker", "name", "market", "entry_price",
                   "entry_date", "quantity", "stop_loss_price", "memo"]


def _write_csv(path: Path, rows: list[dict]) -> None:
    """テスト用 CSV をフィールド名付きで書き出す。"""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ============================================================
# テストクラス
# ============================================================

class EmptyCsvTests(unittest.TestCase):
    """ヘッダーのみの空 CSV を渡したとき、エラーなく 0 件を返す。"""

    def test_empty_csv_returns_zero_and_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "holdings.csv"
            # ヘッダーのみ書き込む（データ行なし）
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=_CSV_FIELDNAMES)
                writer.writeheader()

            conn = _make_temp_db()
            with patch.object(hm, "_HOLDINGS_CSV", csv_path), \
                 patch("src.holdings_monitor.get_connection", _make_fake_connection(conn)):
                result = hm.sync_holdings_from_csv()

        self.assertEqual(result, 0, "空 CSV では 0 件が返るべき")
        rows = conn.execute("SELECT COUNT(*) as cnt FROM holdings").fetchone()
        self.assertEqual(rows["cnt"], 0, "holdings テーブルに行が追加されていない")

    def test_empty_csv_closes_existing_open_holdings(self):
        """データ行なし CSV でも、DB の未決済行は決済済みにする。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "holdings.csv"
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=_CSV_FIELDNAMES)
                writer.writeheader()

            conn = _make_temp_db()
            conn.execute(
                "INSERT INTO holdings (ticker, market, entry_price, is_closed) "
                "VALUES ('NVDA', 'US', 180.0, 0)"
            )
            conn.commit()

            with patch.object(hm, "_HOLDINGS_CSV", csv_path), \
                 patch("src.holdings_monitor.get_connection", _make_fake_connection(conn)):
                hm.sync_holdings_from_csv()

        open_cnt = conn.execute(
            "SELECT COUNT(*) as cnt FROM holdings WHERE is_closed = 0"
        ).fetchone()["cnt"]
        self.assertEqual(open_cnt, 0)
        closed = conn.execute(
            "SELECT is_closed FROM holdings WHERE ticker='NVDA'"
        ).fetchone()
        self.assertEqual(closed["is_closed"], 1)


class DuplicateTickerTests(unittest.TestCase):
    """
    同一ティッカーが CSV に2行ある場合の動作テスト。

    holdings_monitor は重複除去を _dedup_open_holdings で行う。
    CSV に同じ ticker が2回あっても holdings テーブルに1行だけ残ること。
    """

    def test_duplicate_ticker_in_csv_results_in_single_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "holdings.csv"
            _write_csv(csv_path, [
                {"ticker": "7203", "name": "トヨタ", "market": "JP",
                 "entry_price": "3000", "entry_date": "2026-01-10",
                 "quantity": "10", "stop_loss_price": "2760", "memo": ""},
                # 同じ ticker を2行目にも記載（entry_price だけ違う）
                {"ticker": "7203", "name": "トヨタ（修正）", "market": "JP",
                 "entry_price": "3100", "entry_date": "2026-01-11",
                 "quantity": "5", "stop_loss_price": "2852", "memo": ""},
            ])

            conn = _make_temp_db()
            with patch.object(hm, "_HOLDINGS_CSV", csv_path), \
                 patch("src.holdings_monitor.get_connection", _make_fake_connection(conn)):
                result = hm.sync_holdings_from_csv()

        # 返り値は CSV のデータ行数（2行分をパースした）
        self.assertEqual(result, 2)

        # DB には is_closed=0 の行が1件だけのこる（重複解消済み）
        rows = conn.execute(
            "SELECT COUNT(*) as cnt FROM holdings WHERE ticker='7203' AND is_closed=0"
        ).fetchone()
        self.assertEqual(rows["cnt"], 1,
                         "重複ティッカーは1行に集約されるべき")

    def test_duplicate_ticker_second_call_also_stays_single_row(self):
        """sync_holdings_from_csv を2回呼んでも重複しない（冪等性確認）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "holdings.csv"
            _write_csv(csv_path, [
                {"ticker": "6758", "name": "ソニー", "market": "JP",
                 "entry_price": "8000", "entry_date": "2026-02-01",
                 "quantity": "2", "stop_loss_price": "7360", "memo": ""},
            ])

            conn = _make_temp_db()
            fake_conn = _make_fake_connection(conn)
            with patch.object(hm, "_HOLDINGS_CSV", csv_path), \
                 patch("src.holdings_monitor.get_connection", fake_conn):
                hm.sync_holdings_from_csv()
                hm.sync_holdings_from_csv()

        rows = conn.execute(
            "SELECT COUNT(*) as cnt FROM holdings WHERE ticker='6758' AND is_closed=0"
        ).fetchone()
        self.assertEqual(rows["cnt"], 1,
                         "2回呼んでも1行のみ残るべき")


class InvalidFormatTests(unittest.TestCase):
    """
    entry_price が欠落・不正の行はスキップされ、クラッシュしない。
    有効な行は正常に同期される。
    """

    def test_missing_entry_price_skips_row_without_crash(self):
        """entry_price が空文字の行はスキップされ、他の行は同期される。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "holdings.csv"
            _write_csv(csv_path, [
                # entry_price が空（無効行）
                {"ticker": "AAPL", "name": "Apple", "market": "US",
                 "entry_price": "", "entry_date": "2026-01-01",
                 "quantity": "1", "stop_loss_price": "", "memo": ""},
                # 有効な行
                {"ticker": "NVDA", "name": "NVIDIA", "market": "US",
                 "entry_price": "180", "entry_date": "2026-01-02",
                 "quantity": "0.5", "stop_loss_price": "165.6", "memo": ""},
            ])

            conn = _make_temp_db()
            with patch.object(hm, "_HOLDINGS_CSV", csv_path), \
                 patch("src.holdings_monitor.get_connection", _make_fake_connection(conn)):
                # 例外が送出されないことを確認
                try:
                    result = hm.sync_holdings_from_csv()
                except Exception as e:
                    self.fail(f"sync_holdings_from_csv が例外を送出しました: {e}")

        # 有効行（NVDA）だけが同期される
        rows = conn.execute(
            "SELECT ticker FROM holdings WHERE is_closed=0"
        ).fetchall()
        tickers = {r["ticker"] for r in rows}
        self.assertIn("NVDA", tickers)
        self.assertNotIn("AAPL", tickers)

    def test_non_numeric_entry_price_skips_row(self):
        """entry_price が数値でない行はスキップされ、クラッシュしない。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "holdings.csv"
            _write_csv(csv_path, [
                {"ticker": "GOOG", "name": "Alphabet", "market": "US",
                 "entry_price": "abc", "entry_date": "2026-01-01",
                 "quantity": "1", "stop_loss_price": "", "memo": ""},
            ])

            conn = _make_temp_db()
            with patch.object(hm, "_HOLDINGS_CSV", csv_path), \
                 patch("src.holdings_monitor.get_connection", _make_fake_connection(conn)):
                try:
                    result = hm.sync_holdings_from_csv()
                except Exception as e:
                    self.fail(f"sync_holdings_from_csv が例外を送出しました: {e}")

        rows = conn.execute("SELECT COUNT(*) as cnt FROM holdings").fetchone()
        self.assertEqual(rows["cnt"], 0, "不正な行は挿入されない")

    def test_missing_ticker_column_skips_row(self):
        """ticker が空文字の行はスキップされる。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "holdings.csv"
            _write_csv(csv_path, [
                {"ticker": "", "name": "Unknown", "market": "US",
                 "entry_price": "100", "entry_date": "", "quantity": "1",
                 "stop_loss_price": "", "memo": ""},
            ])

            conn = _make_temp_db()
            with patch.object(hm, "_HOLDINGS_CSV", csv_path), \
                 patch("src.holdings_monitor.get_connection", _make_fake_connection(conn)):
                try:
                    result = hm.sync_holdings_from_csv()
                except Exception as e:
                    self.fail(f"sync_holdings_from_csv が例外を送出しました: {e}")

        rows = conn.execute("SELECT COUNT(*) as cnt FROM holdings").fetchone()
        self.assertEqual(rows["cnt"], 0)


class MissingFileTests(unittest.TestCase):
    """ファイルが存在しない場合のテスト。"""

    def test_missing_csv_returns_zero_without_crash(self):
        """
        holdings.csv が存在しない場合、sync_holdings_from_csv は
        例外を送出せず 0 を返す。
        """
        non_existent = Path("/tmp/does_not_exist_holdings_12345.csv")
        # ファイルが存在しないことを念のため確認
        self.assertFalse(non_existent.exists())

        conn = _make_temp_db()
        with patch.object(hm, "_HOLDINGS_CSV", non_existent), \
             patch("src.holdings_monitor.get_connection", _make_fake_connection(conn)):
            try:
                result = hm.sync_holdings_from_csv()
            except Exception as e:
                self.fail(f"sync_holdings_from_csv が例外を送出しました: {e}")

        self.assertEqual(result, 0, "ファイル不在時は 0 を返すべき")
        rows = conn.execute("SELECT COUNT(*) as cnt FROM holdings").fetchone()
        self.assertEqual(rows["cnt"], 0)

    def test_missing_csv_does_not_call_db(self):
        """
        holdings.csv が存在しない場合、DB 操作（get_connection）は呼ばれない。
        """
        non_existent = Path("/tmp/does_not_exist_holdings_99999.csv")
        mock_get_conn = MagicMock()

        with patch.object(hm, "_HOLDINGS_CSV", non_existent), \
             patch("src.holdings_monitor.get_connection", mock_get_conn):
            hm.sync_holdings_from_csv()

        mock_get_conn.assert_not_called()


class RemovedFromCsvTests(unittest.TestCase):
    """CSV から消えた銘柄は DB 上の未決済行を決済済みにする。"""

    def test_open_holding_not_in_csv_is_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "holdings.csv"
            _write_csv(csv_path, [
                {"ticker": "5803", "name": "Fujikura", "market": "JP",
                 "entry_price": "5650", "entry_date": "2026-05-26",
                 "quantity": "1", "stop_loss_price": "", "memo": ""},
            ])

            conn = _make_temp_db()
            conn.execute(
                "INSERT INTO holdings (ticker, name, market, entry_price, is_closed) "
                "VALUES ('GOOG', 'Alphabet', 'US', 194.0, 0)"
            )
            conn.execute(
                "INSERT INTO holdings (ticker, name, market, entry_price, is_closed) "
                "VALUES ('NVDA', 'NVIDIA', 'US', 173.0, 0)"
            )
            conn.commit()

            with patch.object(hm, "_HOLDINGS_CSV", csv_path), \
                 patch("src.holdings_monitor.get_connection", _make_fake_connection(conn)):
                hm.sync_holdings_from_csv()

        open_tickers = {
            r["ticker"]
            for r in conn.execute(
                "SELECT ticker FROM holdings WHERE is_closed = 0"
            ).fetchall()
        }
        self.assertEqual(open_tickers, {"5803"})
        for ticker in ("GOOG", "NVDA"):
            row = conn.execute(
                "SELECT is_closed, close_reason FROM holdings "
                "WHERE ticker = ? AND is_closed = 1",
                (ticker,),
            ).fetchone()
            self.assertIsNotNone(row, f"{ticker} should be closed")
            self.assertEqual(row["close_reason"], "manual")

    def test_closed_history_not_reopened_when_ticker_returns_to_csv(self):
        """決済済み履歴は触らず、CSV 再登録時は新しい未決済行になる。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "holdings.csv"
            _write_csv(csv_path, [
                {"ticker": "GOOG", "name": "Alphabet", "market": "US",
                 "entry_price": "200", "entry_date": "2026-05-01",
                 "quantity": "0.07", "stop_loss_price": "184", "memo": ""},
            ])

            conn = _make_temp_db()
            conn.execute(
                "INSERT INTO holdings (ticker, market, entry_price, is_closed, close_reason) "
                "VALUES ('GOOG', 'US', 150.0, 1, 'manual')"
            )
            conn.commit()

            with patch.object(hm, "_HOLDINGS_CSV", csv_path), \
                 patch("src.holdings_monitor.get_connection", _make_fake_connection(conn)):
                hm.sync_holdings_from_csv()

        closed = conn.execute(
            "SELECT entry_price FROM holdings WHERE ticker='GOOG' AND is_closed=1"
        ).fetchone()
        self.assertAlmostEqual(closed["entry_price"], 150.0)
        open_row = conn.execute(
            "SELECT entry_price FROM holdings WHERE ticker='GOOG' AND is_closed=0"
        ).fetchone()
        self.assertAlmostEqual(open_row["entry_price"], 200.0)


class StopLossDefaultTests(unittest.TestCase):
    """stop_loss_price が未指定の場合、デフォルト計算値が使用される。"""

    def test_default_stop_loss_calculated_when_missing(self):
        """
        stop_loss_price が空のとき、entry_price * (1 - 0.08) が設定される。
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "holdings.csv"
            _write_csv(csv_path, [
                {"ticker": "VT", "name": "Vanguard Total", "market": "US",
                 "entry_price": "100", "entry_date": "2026-01-01",
                 "quantity": "1", "stop_loss_price": "", "memo": ""},
            ])

            conn = _make_temp_db()
            with patch.object(hm, "_HOLDINGS_CSV", csv_path), \
                 patch("src.holdings_monitor.get_connection", _make_fake_connection(conn)):
                hm.sync_holdings_from_csv()

        row = conn.execute(
            "SELECT stop_loss_price FROM holdings WHERE ticker='VT' AND is_closed=0"
        ).fetchone()
        self.assertIsNotNone(row)
        expected = round(100.0 * (1 - 0.08), 2)
        self.assertAlmostEqual(row["stop_loss_price"], expected, places=2,
                               msg="デフォルト損切り価格は entry_price * 0.92")


if __name__ == "__main__":
    unittest.main()
