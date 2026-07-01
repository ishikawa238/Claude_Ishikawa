"""market_hours のテスト"""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from src.market_hours import (
    is_jp_market_open,
    is_us_market_open,
    open_markets,
)

_JST = ZoneInfo("Asia/Tokyo")
_ET = ZoneInfo("America/New_York")


class MarketHoursTests(unittest.TestCase):
    def test_jp_morning_session(self):
        dt = datetime(2026, 7, 1, 10, 0, tzinfo=_JST)  # Wed
        self.assertTrue(is_jp_market_open(dt))

    def test_jp_lunch_break_closed(self):
        dt = datetime(2026, 7, 1, 12, 0, tzinfo=_JST)
        self.assertFalse(is_jp_market_open(dt))

    def test_jp_weekend_closed(self):
        dt = datetime(2026, 7, 4, 10, 0, tzinfo=_JST)  # Sat
        self.assertFalse(is_jp_market_open(dt))

    def test_us_session_weekday(self):
        # 2026-07-01 23:00 JST ≈ 10:00 ET (EDT)
        dt = datetime(2026, 7, 1, 23, 0, tzinfo=_JST)
        et = dt.astimezone(_ET)
        if et.weekday() < 5:
            self.assertTrue(is_us_market_open(dt))

    def test_open_markets_filters(self):
        dt = datetime(2026, 7, 1, 10, 0, tzinfo=_JST)
        self.assertEqual(open_markets(["JP"], dt), ["JP"])
        self.assertEqual(open_markets(["US"], dt), [])


if __name__ == "__main__":
    unittest.main()
