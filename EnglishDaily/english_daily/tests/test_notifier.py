"""notifier モジュールのテスト"""

import sys
import unittest
from datetime import date
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.config import CONFIG  # noqa: E402
from src.notifier import build_morning_blocks, build_reminder_blocks  # noqa: E402


class TestBuildMorningBlocks(unittest.TestCase):

    def test_returns_blocks(self):
        tasks = ["単語: 10個覚える", "リスニング: 1本聴く"]
        blocks = build_morning_blocks(date(2026, 4, 27), tasks, current_streak=5)
        self.assertIsInstance(blocks, list)
        self.assertTrue(len(blocks) >= 4)

    def test_streak_shown_when_positive(self):
        tasks = ["単語: 10個覚える"]
        blocks = build_morning_blocks(date(2026, 4, 27), tasks, current_streak=3)
        header_text = blocks[0]["text"]["text"]
        self.assertIn("3日連続", header_text)

    def test_no_streak_when_zero(self):
        tasks = ["単語: 10個覚える"]
        blocks = build_morning_blocks(date(2026, 4, 27), tasks, current_streak=0)
        header_text = blocks[0]["text"]["text"]
        self.assertNotIn("日連続", header_text)

    def test_weekday_label(self):
        blocks = build_morning_blocks(date(2026, 4, 27), ["タスク"], current_streak=0)
        header_text = blocks[0]["text"]["text"]
        self.assertIn("月", header_text)


class TestBuildReminderBlocks(unittest.TestCase):

    def test_returns_blocks(self):
        uncompleted = [(0, "単語: 10個覚える"), (1, "リスニング: 1本聴く")]
        blocks = build_reminder_blocks(uncompleted, slot_index=0)
        self.assertIsInstance(blocks, list)
        self.assertTrue(len(blocks) >= 4)

    def test_slot_escalation(self):
        uncompleted = [(0, "タスク")]
        blocks_gentle = build_reminder_blocks(uncompleted, slot_index=0)
        blocks_urgent = build_reminder_blocks(uncompleted, slot_index=3)
        header_gentle = blocks_gentle[0]["text"]["text"]
        header_urgent = blocks_urgent[0]["text"]["text"]
        self.assertNotEqual(header_gentle, header_urgent)


if __name__ == "__main__":
    unittest.main()
