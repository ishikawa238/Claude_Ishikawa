import unittest
from types import SimpleNamespace

from scripts.sync_from_moomoo import _get_supported_trade_markets, _select_trade_profile


class GetSupportedTradeMarketsTests(unittest.TestCase):
    def test_skips_missing_all_enum_and_uses_available_markets(self):
        fake_ft = SimpleNamespace(
            TrdMarket=SimpleNamespace(US="US_ENUM", JP="JP_ENUM")
        )

        markets = _get_supported_trade_markets(fake_ft)

        self.assertEqual(markets, [("US", "US_ENUM"), ("JP", "JP_ENUM")])

    def test_handles_missing_jp_enum(self):
        fake_ft = SimpleNamespace(
            TrdMarket=SimpleNamespace(US="US_ENUM")
        )

        markets = _get_supported_trade_markets(fake_ft)

        self.assertEqual(markets, [("US", "US_ENUM")])


class SelectTradeProfileTests(unittest.TestCase):
    def test_auto_falls_back_to_simulate_when_real_is_unavailable(self):
        account_rows = [
            {"trd_env": "SIMULATE", "trdmarket_auth": ["US"]},
        ]

        env_name, markets, unsupported = _select_trade_profile(account_rows, "auto")

        self.assertEqual(env_name, "SIMULATE")
        self.assertEqual(markets, ["US"])
        self.assertEqual(unsupported, [])

    def test_records_unsupported_hk_authority(self):
        account_rows = [
            {"trd_env": "REAL", "trdmarket_auth": ["HK", "JP"]},
        ]

        env_name, markets, unsupported = _select_trade_profile(account_rows, "real")

        self.assertEqual(env_name, "REAL")
        self.assertEqual(markets, ["JP"])
        self.assertEqual(unsupported, ["HK"])


if __name__ == "__main__":
    unittest.main()