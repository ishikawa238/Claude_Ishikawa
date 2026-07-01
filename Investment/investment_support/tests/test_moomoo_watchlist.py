"""moomoo お気に入り同期のユニットテスト（API 呼び出しなし）。"""

import sys
import unittest
from datetime import date
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

from src.moomoo_watchlist import (
    plan_watchlist_sync,
    ticker_to_moomoo_code,
    _days_since,
)


class TestTickerToMoomooCode(unittest.TestCase):
    def test_jp(self):
        self.assertEqual(ticker_to_moomoo_code("7203", "JP"), "JP.7203")

    def test_us(self):
        self.assertEqual(ticker_to_moomoo_code("aapl", "US"), "US.AAPL")


class TestPlanWatchlistSync(unittest.TestCase):
    def _tracked(self, ticker, market, last_candidate, added=None):
        return {
            "ticker": ticker,
            "market": market,
            "moomoo_code": ticker_to_moomoo_code(ticker, market),
            "added_at": added or last_candidate,
            "last_candidate_date": last_candidate,
        }

    def test_add_new_candidate(self):
        plan = plan_watchlist_sync(
            [],
            {"AAA"},
            date(2026, 7, 1),
            market="US",
        )
        self.assertEqual(len(plan["add"]), 1)
        self.assertEqual(plan["add"][0]["ticker"], "AAA")
        self.assertEqual(plan["remove"], [])

    def test_no_remove_within_grace_period(self):
        tracked = [self._tracked("AAA", "US", "2026-06-28")]
        plan = plan_watchlist_sync(
            tracked,
            set(),
            date(2026, 7, 1),
            market="US",
            min_days_before_remove=7,
        )
        self.assertEqual(plan["add"], [])
        self.assertEqual(plan["remove"], [])

    def test_remove_after_grace_period(self):
        tracked = [self._tracked("AAA", "US", "2026-06-20")]
        plan = plan_watchlist_sync(
            tracked,
            set(),
            date(2026, 7, 1),
            market="US",
            min_days_before_remove=7,
        )
        self.assertEqual(len(plan["remove"]), 1)
        self.assertEqual(plan["remove"][0]["ticker"], "AAA")
        self.assertEqual(len(plan["next_rows"]), 0)

    def test_holdings_excluded_from_remove(self):
        tracked = [self._tracked("AAA", "US", "2026-06-20")]
        plan = plan_watchlist_sync(
            tracked,
            set(),
            date(2026, 7, 1),
            market="US",
            min_days_before_remove=7,
            holding_tickers={"AAA"},
        )
        self.assertEqual(plan["remove"], [])

    def test_bookmark_excluded_from_remove(self):
        tracked = [self._tracked("AAA", "US", "2026-06-20")]
        plan = plan_watchlist_sync(
            tracked,
            set(),
            date(2026, 7, 1),
            market="US",
            min_days_before_remove=7,
            bookmark_tickers={"AAA"},
        )
        self.assertEqual(plan["remove"], [])

    def test_update_last_candidate_date(self):
        tracked = [self._tracked("AAA", "US", "2026-06-30")]
        plan = plan_watchlist_sync(
            tracked,
            {"AAA"},
            date(2026, 7, 1),
            market="US",
        )
        self.assertEqual(plan["add"], [])
        self.assertEqual(plan["update"], ["US:AAA"])
        row = plan["next_rows"][0]
        self.assertEqual(row["last_candidate_date"], "2026-07-01")

    def test_other_market_untouched(self):
        tracked = [self._tracked("AAA", "JP", "2026-06-20")]
        plan = plan_watchlist_sync(
            tracked,
            set(),
            date(2026, 7, 1),
            market="US",
            min_days_before_remove=7,
        )
        self.assertEqual(plan["remove"], [])
        self.assertEqual(len(plan["next_rows"]), 1)


class TestDaysSince(unittest.TestCase):
    def test_days(self):
        self.assertEqual(_days_since("2026-06-24", date(2026, 7, 1)), 7)


if __name__ == "__main__":
    unittest.main()
