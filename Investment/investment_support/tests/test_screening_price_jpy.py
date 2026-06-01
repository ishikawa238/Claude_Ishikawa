"""Screening 株価（円）換算のテスト"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

import unittest

from scripts.order_server import _close_price_jpy


class ClosePriceJpyTestCase(unittest.TestCase):
    def test_jp_uses_close_as_is(self):
        self.assertEqual(_close_price_jpy(3200.0, "JP", 150.0), 3200.0)

    def test_us_multiplies_by_usdjpy(self):
        self.assertEqual(_close_price_jpy(100.0, "US", 150.0), 15000.0)

    def test_invalid_close_returns_none(self):
        self.assertIsNone(_close_price_jpy(None, "JP", 150.0))
        self.assertIsNone(_close_price_jpy(0, "US", 150.0))


if __name__ == "__main__":
    unittest.main()
