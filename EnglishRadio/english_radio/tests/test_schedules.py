import unittest
from datetime import datetime

from src import schedules


class SchedulesTest(unittest.TestCase):
    SCHED = [{"label": "M", "start": "07:00", "end": "08:30", "days": "weekdays"}]

    def test_active_within_window(self):
        wed = datetime(2026, 11, 18, 7, 5)
        self.assertEqual(schedules.active_schedule(self.SCHED, wed)["label"], "M")
        self.assertIsNone(schedules.active_schedule(self.SCHED, datetime(2026, 11, 18, 7, 30)))

    def test_weekday_filter(self):
        sat = datetime(2026, 11, 21, 7, 0)
        self.assertIsNone(schedules.active_schedule(self.SCHED, sat))

    def test_duration(self):
        self.assertEqual(schedules.duration_minutes(self.SCHED[0]), 90)

    def test_next_occurrence_skips_weekend(self):
        fri_evening = datetime(2026, 11, 20, 20, 0)
        at, _ = schedules.next_occurrence(self.SCHED, fri_evening)
        self.assertEqual(at, datetime(2026, 11, 23, 7, 0))  # 月曜

    def test_validate_ok(self):
        cleaned = schedules.validate_schedules([
            {"start": "21:00", "end": "21:30"},
            {"label": " A ", "start": "07:00", "end": "08:00", "days": "weekends"},
        ])
        self.assertEqual([c["start"] for c in cleaned], ["07:00", "21:00"])
        self.assertEqual(cleaned[0]["label"], "A")
        self.assertEqual(cleaned[1]["days"], "daily")

    def test_validate_errors(self):
        bad_inputs = [
            [],
            [{"start": "7:00", "end": "08:00"}],
            [{"start": "08:00", "end": "07:00"}],
            [{"start": "25:00", "end": "26:00"}],
            [{"start": "07:00", "end": "08:00", "days": "sometimes"}],
            [{"start": "07:00", "end": "08:00"}, {"start": "07:00", "end": "09:00"}],
            "not a list",
        ]
        for raw in bad_inputs:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                schedules.validate_schedules(raw)


if __name__ == "__main__":
    unittest.main()
