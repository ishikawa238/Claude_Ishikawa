"""trade_signal_log 重複整理 CLI のテスト。"""

import sqlite3
import unittest

from scripts.cleanup_duplicate_trade_signals import cleanup_duplicate_trade_signals


class CleanupDuplicateTradeSignalsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE trade_signal_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_date DATE NOT NULL,
                ticker TEXT NOT NULL,
                market TEXT NOT NULL,
                pattern TEXT NOT NULL
            );
            CREATE TABLE trade_result_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER NOT NULL
            );
            INSERT INTO trade_signal_log (signal_date, ticker, market, pattern)
            VALUES
                ('2026-05-14', 'TEST', 'US', 'A'),
                ('2026-05-14', 'TEST', 'US', 'A'),
                ('2026-05-14', 'OTHER', 'US', 'A');
            INSERT INTO trade_result_log (signal_id) VALUES (2);
            """
        )

    def tearDown(self):
        self.conn.close()

    def test_dry_run_does_not_modify_rows(self):
        result = cleanup_duplicate_trade_signals(self.conn)

        count = self.conn.execute("SELECT COUNT(*) FROM trade_signal_log").fetchone()[0]
        self.assertEqual(result["groups"], 1)
        self.assertEqual(result["duplicate_rows"], 1)
        self.assertEqual(result["deleted_rows"], 0)
        self.assertEqual(count, 3)

    def test_apply_repoints_result_and_deletes_duplicate(self):
        result = cleanup_duplicate_trade_signals(self.conn, apply=True)

        count = self.conn.execute("SELECT COUNT(*) FROM trade_signal_log").fetchone()[0]
        signal_id = self.conn.execute(
            "SELECT signal_id FROM trade_result_log"
        ).fetchone()[0]
        self.assertEqual(result["updated_results"], 1)
        self.assertEqual(result["deleted_rows"], 1)
        self.assertEqual(count, 2)
        self.assertEqual(signal_id, 1)


if __name__ == "__main__":
    unittest.main()
