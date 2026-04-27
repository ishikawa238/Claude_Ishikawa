"""curriculum モジュールのテスト"""

import sys
import unittest
from datetime import date
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.config import CONFIG  # noqa: E402
from src.curriculum import REACTION_EMOJIS, REACTION_NAMES, get_tasks  # noqa: E402


class TestGetTasks(unittest.TestCase):
    """get_tasks() が曜日に応じた正しいタスクを返すことを確認する。"""

    def test_monday_returns_2_tasks(self):
        # 2026-04-27 は月曜
        tasks = get_tasks(date(2026, 4, 27))
        self.assertEqual(len(tasks), 2)

    def test_saturday_returns_4_tasks(self):
        # 2026-05-02 は土曜
        tasks = get_tasks(date(2026, 5, 2))
        self.assertEqual(len(tasks), 4)

    def test_sunday_returns_4_tasks(self):
        # 2026-04-26 は日曜
        tasks = get_tasks(date(2026, 4, 26))
        self.assertEqual(len(tasks), 4)

    def test_every_day_has_vocabulary(self):
        """全曜日に「単語」タスクが含まれる。"""
        for day_offset in range(7):
            d = date(2026, 4, 20 + day_offset)  # 月〜日 (4/20月〜4/26日)
            tasks = get_tasks(d)
            self.assertTrue(
                any("単語" in t for t in tasks),
                f"{d} に単語タスクがありません",
            )


class TestConstants(unittest.TestCase):
    """定数の整合性を確認する。"""

    def test_reaction_names_count(self):
        self.assertEqual(len(REACTION_NAMES), 4)

    def test_reaction_emojis_count(self):
        self.assertEqual(len(REACTION_EMOJIS), 4)


if __name__ == "__main__":
    unittest.main()
