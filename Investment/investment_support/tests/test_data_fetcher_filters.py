"""US銘柄マスタの除外条件テスト。"""

import unittest

from src.data_fetcher import _check_excluded


class CheckExcludedTests(unittest.TestCase):
    def test_common_stock_is_kept(self):
        self.assertEqual(_check_excluded("common stock"), (False, None))

    def test_otc_exchange_is_excluded(self):
        self.assertEqual(
            _check_excluded("common stock", exchange_detail="OTC Pink"),
            (True, "OTC"),
        )

    def test_right_type_is_excluded(self):
        self.assertEqual(_check_excluded("subscription right"), (True, "権利"))

    def test_right_name_suffix_is_excluded_when_type_is_incomplete(self):
        self.assertEqual(
            _check_excluded(
                "common stock",
                name="Amplitech Group, Inc. Series A Right",
            ),
            (True, "権利"),
        )

    def test_unit_type_is_excluded(self):
        self.assertEqual(_check_excluded("unit"), (True, "ユニット"))

    def test_liquidating_name_is_excluded(self):
        self.assertEqual(
            _check_excluded("common stock", name="Example Liquidating Trust"),
            (True, "清算銘柄"),
        )


if __name__ == "__main__":
    unittest.main()
