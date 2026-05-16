"""
paypay_points モジュールのテスト

テスト対象:
- sync_paypay_points_from_csv(): CSV同期
- evaluate_paypay_points(): コース別損益率計算
- get_total_points_value(): ポイント合計
- generate_points_signals(): タイミングシグナル生成
"""

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

_CONFIG_MOCK = {
    "capital": {"total_capital_jpy": "200000", "total_capital_usd": "1500"},
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
    "paypay_points": {"enabled": True},
}

_test_conn = None


def _setup_test_db():
    global _test_conn
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE paypay_points (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            course          TEXT    NOT NULL UNIQUE,
            course_name     TEXT    NOT NULL,
            invested_points INTEGER NOT NULL,
            current_points  INTEGER NOT NULL,
            last_updated    DATE    NOT NULL,
            created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE market_environment (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            DATE    NOT NULL UNIQUE,
            jp_above_ma25   INTEGER,
            us_above_ma25   INTEGER,
            mode            TEXT    NOT NULL,
            n225_close      REAL,
            n225_ma25       REAL,
            sp500_close     REAL,
            sp500_ma25      REAL,
            created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE index_snapshot (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            index_code  TEXT    NOT NULL,
            date        DATE    NOT NULL,
            close       REAL    NOT NULL,
            ma25        REAL,
            ma75        REAL,
            ma200       REAL,
            ma25_5d_ago REAL,
            created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX idx_index_snapshot_code_date ON index_snapshot(index_code, date)"
    )
    _test_conn = conn
    return conn


@contextmanager
def _mock_connection():
    yield _test_conn


@patch.dict("src.config.CONFIG", _CONFIG_MOCK)
@patch("src.data_store.get_connection", _mock_connection)
@patch("src.data_store._shared_get_connection", _mock_connection)
class TestPayPayPoints(unittest.TestCase):

    def setUp(self):
        _setup_test_db()
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        global _test_conn
        if _test_conn:
            _test_conn.close()
            _test_conn = None

    # ────────────────────────────────────────
    # sync_paypay_points_from_csv
    # ────────────────────────────────────────

    def test_sync_from_csv(self):
        """正常な CSV の同期"""
        csv_path = Path(self._tmpdir) / "paypay_points.csv"
        csv_path.write_text(
            "course,invested_points,current_points,last_updated\n"
            "standard,5000,5230,2026-05-16\n"
            "technology,3000,2850,2026-05-16\n",
            encoding="utf-8",
        )
        from src.paypay_points import sync_paypay_points_from_csv
        with patch("src.paypay_points._POINTS_CSV", csv_path):
            count = sync_paypay_points_from_csv()

        self.assertEqual(count, 2)
        rows = _test_conn.execute("SELECT * FROM paypay_points ORDER BY course").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["course"], "standard")
        self.assertEqual(rows[0]["invested_points"], 5000)
        self.assertEqual(rows[0]["current_points"], 5230)

    def test_sync_csv_not_found(self):
        """CSV 不在時にエラーなく 0 を返す"""
        csv_path = Path(self._tmpdir) / "nonexistent.csv"
        from src.paypay_points import sync_paypay_points_from_csv
        with patch("src.paypay_points._POINTS_CSV", csv_path):
            count = sync_paypay_points_from_csv()
        self.assertEqual(count, 0)

    def test_sync_updates_existing(self):
        """再同期で既存行が UPDATE される"""
        csv_path = Path(self._tmpdir) / "paypay_points.csv"
        csv_path.write_text(
            "course,invested_points,current_points,last_updated\n"
            "standard,5000,5230,2026-05-16\n",
            encoding="utf-8",
        )
        from src.paypay_points import sync_paypay_points_from_csv
        with patch("src.paypay_points._POINTS_CSV", csv_path):
            sync_paypay_points_from_csv()

        # 更新
        csv_path.write_text(
            "course,invested_points,current_points,last_updated\n"
            "standard,5000,5500,2026-05-17\n",
            encoding="utf-8",
        )
        with patch("src.paypay_points._POINTS_CSV", csv_path):
            sync_paypay_points_from_csv()

        rows = _test_conn.execute("SELECT * FROM paypay_points").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["current_points"], 5500)
        self.assertEqual(rows[0]["last_updated"], "2026-05-17")

    def test_sync_skips_unknown_course(self):
        """不明なコースはスキップされる"""
        csv_path = Path(self._tmpdir) / "paypay_points.csv"
        csv_path.write_text(
            "course,invested_points,current_points,last_updated\n"
            "unknown_course,1000,1000,2026-05-16\n"
            "standard,5000,5230,2026-05-16\n",
            encoding="utf-8",
        )
        from src.paypay_points import sync_paypay_points_from_csv
        with patch("src.paypay_points._POINTS_CSV", csv_path):
            count = sync_paypay_points_from_csv()
        self.assertEqual(count, 1)

    # ────────────────────────────────────────
    # evaluate_paypay_points
    # ────────────────────────────────────────

    def test_evaluate_returns(self):
        """損益率が正しく計算される"""
        _test_conn.execute(
            "INSERT INTO paypay_points (course, course_name, invested_points, current_points, last_updated) "
            "VALUES (?, ?, ?, ?, ?)",
            ("standard", "スタンダード", 5000, 5500, date.today().isoformat()),
        )
        _test_conn.commit()

        from src.paypay_points import evaluate_paypay_points
        result = evaluate_paypay_points()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["return_pct"], 10.0)
        self.assertEqual(result[0]["return_points"], 500)
        self.assertFalse(result[0]["stale"])

    def test_evaluate_negative_return(self):
        """マイナスリターンの計算"""
        _test_conn.execute(
            "INSERT INTO paypay_points (course, course_name, invested_points, current_points, last_updated) "
            "VALUES (?, ?, ?, ?, ?)",
            ("technology", "テクノロジー", 3000, 2850, date.today().isoformat()),
        )
        _test_conn.commit()

        from src.paypay_points import evaluate_paypay_points
        result = evaluate_paypay_points()

        self.assertEqual(result[0]["return_pct"], -5.0)
        self.assertEqual(result[0]["return_points"], -150)

    def test_evaluate_stale_detection(self):
        """3日以上前の last_updated で stale=True"""
        old_date = (date.today() - timedelta(days=5)).isoformat()
        _test_conn.execute(
            "INSERT INTO paypay_points (course, course_name, invested_points, current_points, last_updated) "
            "VALUES (?, ?, ?, ?, ?)",
            ("standard", "スタンダード", 5000, 5230, old_date),
        )
        _test_conn.commit()

        from src.paypay_points import evaluate_paypay_points
        result = evaluate_paypay_points()

        self.assertTrue(result[0]["stale"])

    # ────────────────────────────────────────
    # get_total_points_value
    # ────────────────────────────────────────

    def test_get_total_points_value(self):
        """全コース合計を正しく返す"""
        _test_conn.execute(
            "INSERT INTO paypay_points (course, course_name, invested_points, current_points, last_updated) "
            "VALUES (?, ?, ?, ?, ?)",
            ("standard", "スタンダード", 5000, 5230, "2026-05-16"),
        )
        _test_conn.execute(
            "INSERT INTO paypay_points (course, course_name, invested_points, current_points, last_updated) "
            "VALUES (?, ?, ?, ?, ?)",
            ("technology", "テクノロジー", 3000, 2850, "2026-05-16"),
        )
        _test_conn.commit()

        from src.paypay_points import get_total_points_value
        total = get_total_points_value()
        self.assertEqual(total, 8080)

    def test_get_total_points_value_empty(self):
        """テーブルが空の場合は 0"""
        from src.paypay_points import get_total_points_value
        total = get_total_points_value()
        self.assertEqual(total, 0)

    # ────────────────────────────────────────
    # generate_points_signals
    # ────────────────────────────────────────

    def test_generate_signals_sp500_below_ma25(self):
        """S&P 500 が MA25 以下の場合、standard は caution"""
        _test_conn.execute(
            "INSERT INTO paypay_points (course, course_name, invested_points, current_points, last_updated) "
            "VALUES (?, ?, ?, ?, ?)",
            ("standard", "スタンダード", 5000, 5230, "2026-05-16"),
        )
        _test_conn.execute(
            "INSERT INTO market_environment (date, mode, jp_above_ma25, us_above_ma25, "
            "sp500_close, sp500_ma25, n225_close, n225_ma25) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-05-16", "caution", 1, 0, 5200, 5300, 28500, 28300),
        )
        _test_conn.commit()

        from src.paypay_points import generate_points_signals
        signals = generate_points_signals(date(2026, 5, 16))

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["signal"], "caution")

    def test_generate_signals_sp500_above_ma25(self):
        """S&P 500 が MA25 以上の場合、standard は good"""
        _test_conn.execute(
            "INSERT INTO paypay_points (course, course_name, invested_points, current_points, last_updated) "
            "VALUES (?, ?, ?, ?, ?)",
            ("standard", "スタンダード", 5000, 5230, "2026-05-16"),
        )
        _test_conn.execute(
            "INSERT INTO market_environment (date, mode, jp_above_ma25, us_above_ma25, "
            "sp500_close, sp500_ma25, n225_close, n225_ma25) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-05-16", "normal", 1, 1, 5400, 5300, 28500, 28300),
        )
        _test_conn.commit()

        from src.paypay_points import generate_points_signals
        signals = generate_points_signals(date(2026, 5, 16))

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["signal"], "good")

    def test_generate_signals_nasdaq_course(self):
        """NASDAQ 連動コースは index_snapshot から判定"""
        _test_conn.execute(
            "INSERT INTO paypay_points (course, course_name, invested_points, current_points, last_updated) "
            "VALUES (?, ?, ?, ?, ?)",
            ("tech_challenge", "テクノロジーチャレンジ", 2000, 2340, "2026-05-16"),
        )
        _test_conn.execute(
            "INSERT INTO market_environment (date, mode, jp_above_ma25, us_above_ma25, "
            "sp500_close, sp500_ma25, n225_close, n225_ma25) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-05-16", "normal", 1, 1, 5400, 5300, 28500, 28300),
        )
        # NASDAQ: close < MA25 → tech_challenge (3x) は caution
        _test_conn.execute(
            "INSERT INTO index_snapshot (index_code, date, close, ma25, ma75) "
            "VALUES (?, ?, ?, ?, ?)",
            ("NASDAQ", "2026-05-16", 16000, 16500, 16200),
        )
        _test_conn.commit()

        from src.paypay_points import generate_points_signals
        signals = generate_points_signals(date(2026, 5, 16))

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["signal"], "caution")
        self.assertIn("3倍レバ", signals[0]["reason"])

    def test_generate_signals_empty(self):
        """保有コースなしの場合は空リスト"""
        from src.paypay_points import generate_points_signals
        signals = generate_points_signals(date(2026, 5, 16))
        self.assertEqual(signals, [])


if __name__ == "__main__":
    unittest.main()
