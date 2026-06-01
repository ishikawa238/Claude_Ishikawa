"""shared.batch の stale running 回収テスト。"""

import sqlite3
import unittest
from datetime import datetime

from shared.batch import mark_stale_running_batches, start_batch_log


class StaleRunningBatchTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE batch_run_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date DATE NOT NULL,
                batch_type TEXT NOT NULL,
                start_time DATETIME,
                end_time DATETIME,
                status TEXT
            )
            """
        )

    def tearDown(self):
        self.conn.close()

    def test_marks_only_stale_rows_of_same_batch_type(self):
        self.conn.executemany(
            """
            INSERT INTO batch_run_log (run_date, batch_type, start_time, status)
            VALUES (?, ?, ?, 'running')
            """,
            [
                ("2026-06-01", "morning_all", "2026-06-01 05:00:00"),
                ("2026-06-01", "morning_all", "2026-06-01 09:30:00"),
                ("2026-06-01", "evening_jp", "2026-06-01 05:00:00"),
            ],
        )

        changed = mark_stale_running_batches(
            self.conn,
            "morning_all",
            stale_after_minutes=180,
            now=datetime(2026, 6, 1, 10, 0, 0),
        )

        rows = self.conn.execute(
            "SELECT batch_type, start_time, status FROM batch_run_log ORDER BY id"
        ).fetchall()
        self.assertEqual(changed, 1)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertEqual(rows[1]["status"], "running")
        self.assertEqual(rows[2]["status"], "running")

    def test_start_batch_log_recovers_stale_row_before_insert(self):
        self.conn.execute(
            """
            INSERT INTO batch_run_log (run_date, batch_type, start_time, status)
            VALUES ('2020-01-01', 'monthly', '2020-01-01 00:00:00', 'running')
            """
        )

        new_id = start_batch_log(self.conn, "monthly", "2026-06-01")

        old = self.conn.execute(
            "SELECT status, end_time FROM batch_run_log WHERE id = 1"
        ).fetchone()
        new = self.conn.execute(
            "SELECT status FROM batch_run_log WHERE id = ?",
            (new_id,),
        ).fetchone()
        self.assertEqual(old["status"], "failed")
        self.assertIsNotNone(old["end_time"])
        self.assertEqual(new["status"], "running")


if __name__ == "__main__":
    unittest.main()
