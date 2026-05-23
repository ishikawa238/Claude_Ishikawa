"""
yfinance_fetcher.py のテスト（モック使用、ネットワーク不要）
"""

import unittest
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd

from src.yfinance_fetcher import (
    DailyPriceFetchResult,
    JP_PRICE_SOURCE,
    _yf_ticker,
    fetch_daily_yf,
    fetch_daily_yf_with_status,
    fetch_history_bulk_yf,
    fetch_history_yf,
)


class TestYfTicker(unittest.TestCase):
  def test_jp_suffix(self):
    self.assertEqual(_yf_ticker("7203", "JP"), "7203.T")

  def test_us_unchanged(self):
    self.assertEqual(_yf_ticker("AAPL", "US"), "AAPL")


class TestFetchHistoryYf(unittest.TestCase):
  @patch("yfinance.download")
  def test_returns_rows_on_success(self, mock_download):
    mock_download.return_value = pd.DataFrame(
      {"Open": [100], "High": [101], "Low": [99], "Close": [100], "Volume": [1000]},
      index=pd.to_datetime(["2026-05-20"]),
    )
    rows = fetch_history_yf("7203", "JP", date(2026, 5, 20), date(2026, 5, 20))
    self.assertEqual(len(rows), 1)
    self.assertEqual(rows[0]["ticker"], "7203")
    self.assertEqual(rows[0]["close"], 100.0)

  @patch("yfinance.download", side_effect=TimeoutError("timeout"))
  def test_returns_empty_on_exception(self, _mock_download):
    rows = fetch_history_yf("7203", "JP", date(2026, 5, 20), date(2026, 5, 20))
    self.assertEqual(rows, [])

  @patch("yfinance.download")
  def test_returns_empty_on_empty_dataframe(self, mock_download):
    mock_download.return_value = pd.DataFrame()
    rows = fetch_history_yf("7203", "JP", date(2026, 5, 20), date(2026, 5, 20))
    self.assertEqual(rows, [])


class TestFetchHistoryBulkYf(unittest.TestCase):
  @patch("src.yfinance_fetcher.fetch_history_bulk_yf")
  def test_partial_failure_via_daily_status(self, mock_bulk):
    """一部失敗時は DailyPriceFetchResult が partial になる。"""
    target_str = "2026-05-20"
    mock_bulk.return_value = (
      [{"ticker": "7203", "date": target_str, "close": 100.0}],
      ["6758"],
    )
    result = fetch_daily_yf_with_status(
      [("7203", "JP"), ("6758", "JP"), ("9984", "JP"), ("9432", "JP")],
      date(2026, 5, 20),
    )
    self.assertEqual(result.status, "partial")
    self.assertIn("一部失敗", result.alert_message or "")

  @patch("yfinance.download", side_effect=RuntimeError("api down"))
  def test_batch_exception_marks_all_failed(self, _mock_download):
    tickers = [("7203", "JP"), ("6758", "JP")]
    rows, failed = fetch_history_bulk_yf(tickers, date(2026, 5, 20), date(2026, 5, 20))
    self.assertEqual(rows, [])
    self.assertEqual(set(failed), {"7203", "6758"})

  @patch("src.yfinance_fetcher.time.sleep")
  @patch("yfinance.download")
  def test_empty_batch_marks_all_failed(self, mock_download, _mock_sleep):
    mock_download.return_value = pd.DataFrame()
    tickers = [("7203", "JP")]
    rows, failed = fetch_history_bulk_yf(tickers, date(2026, 5, 20), date(2026, 5, 20))
    self.assertEqual(rows, [])
    self.assertEqual(failed, ["7203"])


class TestFetchDailyYfWithStatus(unittest.TestCase):
  @patch("src.yfinance_fetcher.fetch_history_bulk_yf")
  def test_ok_when_target_date_coverage_met(self, mock_bulk):
    target = date(2026, 5, 20)
    target_str = "2026-05-20"
    rows = [
      {"ticker": f"{i:04d}", "date": target_str, "close": 100.0}
      for i in range(3)
    ]
    mock_bulk.return_value = (rows, [])
    result = fetch_daily_yf_with_status(
      [("0001", "JP"), ("0002", "JP"), ("0003", "JP")], target,
    )
    self.assertEqual(result.status, "ok")
    self.assertEqual(len(result.rows), 3)
    self.assertIsNone(result.alert_message)

  @patch("src.yfinance_fetcher.fetch_history_bulk_yf")
  def test_failed_when_no_rows(self, mock_bulk):
    mock_bulk.return_value = ([], ["7203", "6758"])
    result = fetch_daily_yf_with_status([("7203", "JP"), ("6758", "JP")], date(2026, 5, 20))
    self.assertEqual(result.status, "failed")
    self.assertIn("JP価格データ取得失敗", result.alert_message or "")

  @patch("src.yfinance_fetcher.fetch_history_bulk_yf")
  def test_partial_when_some_failed(self, mock_bulk):
    target_str = "2026-05-20"
    rows = [{"ticker": "7203", "date": target_str, "close": 100.0}] * 2
    mock_bulk.return_value = (rows, ["6758"])
    result = fetch_daily_yf_with_status(
      [("7203", "JP"), ("6758", "JP"), ("9984", "JP"), ("9432", "JP")],
      date(2026, 5, 20),
    )
    self.assertEqual(result.status, "partial")
    self.assertIn("一部失敗", result.alert_message or "")

  @patch("src.yfinance_fetcher.fetch_history_bulk_yf")
  def test_fetch_daily_yf_wrapper_returns_rows_only(self, mock_bulk):
    target_str = "2026-05-20"
    mock_bulk.return_value = (
      [{"ticker": "7203", "date": target_str, "close": 100.0}],
      [],
    )
    rows = fetch_daily_yf([("7203", "JP")], date(2026, 5, 20))
    self.assertEqual(len(rows), 1)

  def test_empty_ticker_list(self):
    result = fetch_daily_yf_with_status([], date(2026, 5, 20))
    self.assertEqual(result.status, "ok")
    self.assertEqual(result.source, JP_PRICE_SOURCE)


class TestDailyPriceFetchResult(unittest.TestCase):
  def test_failed_count_property(self):
    r = DailyPriceFetchResult(
      rows=[], total_tickers=10, tickers_with_target_date=0,
      failed_tickers=["a", "b"], status="partial",
    )
    self.assertEqual(r.failed_count, 2)


if __name__ == "__main__":
  unittest.main()
