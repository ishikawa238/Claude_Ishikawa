"""screening_bookmarks モジュールのテスト"""

import csv
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT.parent.parent))
sys.path.insert(0, str(_PROJECT_ROOT))

import src.config as _src_config

_src_config.CONFIG = {
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
    "logging": {"log_dir": "/tmp", "log_level": "INFO"},
}

import src.screening_bookmarks as sb


class ScreeningBookmarksTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._csv = Path(self._tmpdir.name) / "screening_bookmarks.csv"
        with open(self._csv, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=sb._CSV_FIELDS)
            writer.writeheader()
        self._patch = patch.object(sb, "_BOOKMARKS_CSV", self._csv)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmpdir.cleanup()

    def test_toggle_add_and_remove(self):
        r1 = sb.toggle_bookmark("7203", name="トヨタ", market="JP")
        self.assertTrue(r1["bookmarked"])
        self.assertEqual(r1["count"], 1)
        self.assertTrue(sb.is_bookmarked("7203"))

        r2 = sb.toggle_bookmark("7203")
        self.assertFalse(r2["bookmarked"])
        self.assertEqual(r2["count"], 0)
        self.assertFalse(sb.is_bookmarked("7203"))

    def test_ticker_normalized_uppercase(self):
        sb.toggle_bookmark("aapl", market="US")
        self.assertTrue(sb.is_bookmarked("AAPL"))
        items = sb.load_bookmarks()
        self.assertEqual(items[0]["ticker"], "AAPL")


if __name__ == "__main__":
    unittest.main()
