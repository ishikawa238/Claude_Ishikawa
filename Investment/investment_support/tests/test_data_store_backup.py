"""data_store のDBバックアップテスト。"""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from src import data_store


class BackupDatabaseTests(unittest.TestCase):
    def test_backup_includes_committed_rows_still_in_wal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "investment.db"
            backup_dir = root / "backups"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA wal_autocheckpoint = 0")
                conn.execute("CREATE TABLE sample (value TEXT)")
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("INSERT INTO sample (value) VALUES ('from-wal')")
                conn.commit()

                with (
                    patch.object(data_store, "_DB_PATH", db_path),
                    patch.object(data_store, "_BACKUP_DIR", backup_dir),
                    patch.object(data_store, "_BACKUP_RETENTION_DAYS", 7),
                ):
                    self.assertTrue(data_store.backup_database())

                [backup_path] = list(backup_dir.glob("investment_*.db"))
                with closing(sqlite3.connect(backup_path)) as snapshot:
                    row = snapshot.execute("SELECT value FROM sample").fetchone()
                self.assertEqual(row[0], "from-wal")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
