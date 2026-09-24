"""moomoo お気に入り同期のユニットテスト（API 呼び出しなし）。"""

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

from src import moomoo_watchlist
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


class TestModifyRateLimit(unittest.TestCase):
    """modify_user_security の「30 秒に 10 回まで」を守ること。"""

    def setUp(self):
        moomoo_watchlist._modify_call_times.clear()

    def tearDown(self):
        moomoo_watchlist._modify_call_times.clear()

    @patch("src.moomoo_watchlist.time.sleep")
    @patch("src.moomoo_watchlist.time.monotonic", return_value=100.0)
    def test_waits_on_eleventh_call(self, _mono, mock_sleep):
        for _ in range(10):
            moomoo_watchlist._wait_for_modify_slot()
        mock_sleep.assert_not_called()
        moomoo_watchlist._wait_for_modify_slot()
        mock_sleep.assert_called_once()
        self.assertAlmostEqual(mock_sleep.call_args[0][0], moomoo_watchlist._API_WINDOW_SEC)

    @patch("src.moomoo_watchlist.time.sleep")
    @patch("src.moomoo_watchlist.time.monotonic")
    def test_no_wait_after_window_passes(self, mock_mono, mock_sleep):
        mock_mono.return_value = 100.0
        for _ in range(10):
            moomoo_watchlist._wait_for_modify_slot()
        mock_mono.return_value = 100.0 + moomoo_watchlist._API_WINDOW_SEC
        moomoo_watchlist._wait_for_modify_slot()
        mock_sleep.assert_not_called()


class TestSyncWatchlistRemoveFailure(unittest.TestCase):
    """追加が成功し削除が全件失敗した場合も、削除失敗分を追跡 CSV に残す。"""

    def test_failed_removal_is_kept(self):
        old = {"ticker": "OLD", "market": "US", "moomoo_code": "US.OLD",
               "added_at": "2026-09-01", "last_candidate_date": "2026-09-01"}
        new = {"ticker": "NEW", "market": "US", "moomoo_code": "US.NEW",
               "added_at": "2026-09-24", "last_candidate_date": "2026-09-24"}
        plan = {"add": [new], "remove": [old], "update": [], "next_rows": [new]}
        save_mock = MagicMock()
        with patch.multiple(
            "src.moomoo_watchlist",
            is_port_open=MagicMock(return_value=True),
            _get_watchlist_cfg=MagicMock(return_value={"group_name": "システム候補"}),
            fetch_candidate_tickers=MagicMock(return_value={"NEW"}),
            load_tracked_rows=MagicMock(return_value=[old]),
            plan_watchlist_sync=MagicMock(return_value=plan),
            _fetch_holding_tickers=MagicMock(return_value=set()),
            _fetch_bookmark_tickers=MagicMock(return_value=set()),
            _group_exists=MagicMock(return_value=True),
            _modify_codes=MagicMock(side_effect=[
                (["US.NEW"], []),
                ([], [("US.OLD", "too frequent")]),
            ]),
            save_tracked_rows=save_mock,
        ):
            result = moomoo_watchlist.sync_watchlist("US", date(2026, 9, 24))

        self.assertEqual(result["added"], 1)
        self.assertEqual(result["removed"], 0)
        saved = save_mock.call_args[0][0]
        self.assertEqual({r["ticker"] for r in saved}, {"NEW", "OLD"})


if __name__ == "__main__":
    unittest.main()
