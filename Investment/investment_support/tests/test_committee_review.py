"""
committee_review モジュールのテスト

テスト対象:
- evaluate_verdicts(): N 営業日リターン、方向の正否、損切り/利確の先着
- evaluate_baseline(): 全エントリーセットの成績
- summarize(): 判定別の集計
"""

import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

_IMPORT_CONFIG = {
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
    "logging": {"log_dir": "/tmp", "log_level": "INFO"},
}

with patch("src.config.CONFIG", _IMPORT_CONFIG):
    from src import committee_review as cr


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE price_history (
            ticker TEXT, date DATE, open REAL, high REAL, low REAL, close REAL,
            is_holiday INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE committee_verdict_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, verdict_date DATE, batch_type TEXT,
            ticker TEXT, market TEXT, subject_type TEXT, verdict TEXT, confidence REAL, status TEXT
        );
        CREATE TABLE trade_signal_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, signal_date DATE, ticker TEXT, market TEXT,
            stop_loss_price REAL, target_price REAL
        );
    """)
    return conn


def _add_prices(conn, ticker: str, rows: list[tuple]) -> None:
    """rows: (date, high, low, close)"""
    conn.executemany(
        "INSERT INTO price_history (ticker, date, high, low, close) VALUES (?, ?, ?, ?, ?)",
        [(ticker, d, h, lo, c) for d, h, lo, c in rows],
    )


def _add_verdict(conn, ticker: str, verdict: str, subject_type: str = "buy_candidate",
                 date_str: str = "2026-10-01", status: str = "full") -> None:
    conn.execute(
        "INSERT INTO committee_verdict_log (verdict_date, batch_type, ticker, market, subject_type, "
        "verdict, confidence, status) VALUES (?, 'evening', ?, 'JP', ?, ?, 0.7, ?)",
        (date_str, ticker, subject_type, verdict, status),
    )


# 基準日 10/01 終値 100、以降 5 営業日
_RISING = [
    ("2026-10-01", 101, 99, 100),
    ("2026-10-02", 103, 100, 102),
    ("2026-10-05", 105, 101, 104),
    ("2026-10-06", 106, 103, 105),
    ("2026-10-07", 108, 104, 107),
    ("2026-10-08", 112, 106, 110),
]
_FALLING = [
    ("2026-10-01", 101, 99, 100),
    ("2026-10-02", 100, 96, 97),
    ("2026-10-05", 97, 93, 95),
    ("2026-10-06", 96, 91, 92),
    ("2026-10-07", 93, 89, 90),
    ("2026-10-08", 91, 88, 89),
]


class CommitteeReviewTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_db()

        @contextmanager
        def _conn():
            yield self.conn

        self._patcher = patch.object(cr, "get_connection", _conn)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def test_buy_verdict_forward_return_and_correctness(self):
        _add_prices(self.conn, "AAA", _RISING)
        _add_verdict(self.conn, "AAA", "BUY")
        ev = cr.evaluate_verdicts(horizons=(5,))[0]
        self.assertAlmostEqual(ev["ret_5"], 0.10)
        self.assertTrue(ev["correct_5"])

    def test_skip_verdict_is_correct_when_price_falls(self):
        _add_prices(self.conn, "BBB", _FALLING)
        _add_verdict(self.conn, "BBB", "SKIP")
        ev = cr.evaluate_verdicts(horizons=(5,))[0]
        self.assertAlmostEqual(ev["ret_5"], -0.11)
        self.assertTrue(ev["correct_5"])

    def test_holding_sell_is_correct_when_price_falls(self):
        _add_prices(self.conn, "CCC", _FALLING)
        _add_verdict(self.conn, "CCC", "SELL", subject_type="holding")
        ev = cr.evaluate_verdicts(horizons=(5,))[0]
        self.assertTrue(ev["correct_5"])
        self.assertIsNone(ev["outcome"])  # 保有は損切り/利確の先着を評価しない

    def test_horizon_not_elapsed_returns_none(self):
        _add_prices(self.conn, "AAA", _RISING[:3])
        _add_verdict(self.conn, "AAA", "BUY")
        ev = cr.evaluate_verdicts(horizons=(5,))[0]
        self.assertIsNone(ev["ret_5"])
        self.assertIsNone(ev["correct_5"])

    def test_failed_and_budget_skipped_verdicts_are_excluded(self):
        _add_prices(self.conn, "AAA", _RISING)
        _add_verdict(self.conn, "AAA", None, status="skipped_budget")
        _add_verdict(self.conn, "AAA", "BUY", status="failed")
        self.assertEqual(cr.evaluate_verdicts(horizons=(5,)), [])

    def test_first_touch_target(self):
        _add_prices(self.conn, "AAA", _RISING)
        self.conn.execute(
            "INSERT INTO trade_signal_log (signal_date, ticker, market, stop_loss_price, target_price) "
            "VALUES ('2026-10-01', 'AAA', 'JP', 95, 106)"
        )
        _add_verdict(self.conn, "AAA", "BUY")
        self.assertEqual(cr.evaluate_verdicts(horizons=(5,))[0]["outcome"], "target")

    def test_first_touch_same_day_counts_as_stop(self):
        _add_prices(self.conn, "AAA", [("2026-10-01", 101, 99, 100), ("2026-10-02", 120, 80, 100)])
        self.conn.execute(
            "INSERT INTO trade_signal_log (signal_date, ticker, market, stop_loss_price, target_price) "
            "VALUES ('2026-10-01', 'AAA', 'JP', 95, 110)"
        )
        _add_verdict(self.conn, "AAA", "BUY")
        self.assertEqual(cr.evaluate_verdicts(horizons=(1,))[0]["outcome"], "stop")

    def test_baseline_and_summary(self):
        _add_prices(self.conn, "AAA", _RISING)
        _add_prices(self.conn, "BBB", _FALLING)
        self.conn.executemany(
            "INSERT INTO trade_signal_log (signal_date, ticker, market, stop_loss_price, target_price) "
            "VALUES ('2026-10-01', ?, 'JP', ?, ?)",
            [("AAA", 95, 106), ("BBB", 92, 116)],
        )
        _add_verdict(self.conn, "AAA", "BUY")
        _add_verdict(self.conn, "BBB", "BUY")

        base = cr.summarize(cr.evaluate_baseline(horizons=(5,)), (5,))["ALL"]
        self.assertEqual(base["count"], 2)
        self.assertAlmostEqual(base["avg_5"], -0.5)  # (+10% - 11%) / 2
        self.assertEqual(base["win_5"], 50.0)
        self.assertEqual((base["target"], base["stop"]), (1, 1))

        verdicts = cr.evaluate_verdicts(horizons=(5,))
        by_verdict = cr.summarize(verdicts, (5,), key=lambda e: e["verdict"])
        self.assertEqual(by_verdict["BUY"]["acc_5"], 50.0)

        report = cr.format_report(verdicts, cr.evaluate_baseline(horizons=(5,)), (5,))
        self.assertIn("buy_candidate:BUY", report)
        self.assertIn("ベースライン", report)

    def test_split_suspect_is_excluded(self):
        """分割未調整（4:1 で終値が 1/4）の期間は評価しない"""
        rows = [("2026-10-01", 101, 99, 100), ("2026-10-02", 26, 24, 25)] + [
            (d, 26, 24, 25) for d in ("2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08")
        ]
        _add_prices(self.conn, "SPL", rows)
        _add_verdict(self.conn, "SPL", "BUY")
        ev = cr.evaluate_verdicts(horizons=(5,))[0]
        self.assertTrue(ev["split_suspect"])
        self.assertIsNone(ev["ret_5"])
        self.assertEqual(cr.summarize([ev], (5,))["ALL"]["split_suspect"], 1)

    def test_empty_report(self):
        report = cr.format_report([], [], (5, 20))
        self.assertIn("評価対象の判定がありません", report)


if __name__ == "__main__":
    unittest.main()
