"""J-Quants 認証・V2 マッピングのテスト"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

import src.config as _src_config
import src.jquants_fetcher as jq

_CONFIG = {
    "jquants": {
        "api_key": "test-api-key",
        "auth_type": "api_key",
        "request_interval_sec": 0,
    },
}


class JQuantsFetcherTestCase(unittest.TestCase):
    def setUp(self):
        _src_config.CONFIG = _CONFIG
        jq.CONFIG = _CONFIG
        jq._JQ = _CONFIG["jquants"]
        jq._API_KEY = "test-api-key"
        jq._AUTH_TYPE = "api_key"

    def test_create_client_v2(self):
        with patch("src.jquants_fetcher.jquantsapi.ClientV2") as mock_cls:
            mock_cls.return_value = MagicMock()
            client, ver = jq._create_client()
            self.assertEqual(ver, "v2")
            mock_cls.assert_called_once_with(api_key="test-api-key")

    def test_fetch_latest_statement_v2(self):
        client = MagicMock()
        client.get_fin_summary.return_value = pd.DataFrame([{
            "DiscDate": pd.Timestamp("2026-03-01"),
            "CurPerType": "FY",
            "CurFYEn": pd.Timestamp("2026-03-31"),
            "Sales": 100.0,
            "OP": 10.0,
            "OdP": 9.0,
            "NP": 8.0,
            "EPS": 50.0,
            "BPS": 500.0,
            "ShOutFY": 1_000_000.0,
            "DivAnn": 2.0,
        }])
        with patch.object(jq, "_get_latest_close", return_value=1000.0):
            row = jq._fetch_latest_statement(client, "v2", "7203", "2026-05-30")
        self.assertIsNotNone(row)
        self.assertEqual(row["ticker"], "7203")
        self.assertEqual(row["shares_outstanding"], 1_000_000.0)
        client.get_fin_summary.assert_called_once_with(code="72030")

    def test_is_auth_failure_detects_refresh_400(self):
        exc = Exception(
            "400 Client Error: Bad Request for url: "
            "https://api.jquants.com/v1/token/auth_refresh?refreshtoken=xxx"
        )
        self.assertTrue(jq._is_auth_failure(exc))

    def test_is_plan_restriction_not_auth(self):
        exc = Exception(
            "403 for url: https://api.jquants.com/v2/bulk/list "
            "body: This API is not available on your subscription."
        )
        self.assertTrue(jq._is_plan_restriction(exc))
        self.assertFalse(jq._is_auth_failure(exc))

    def test_is_auth_failure_not_ticker_code_in_url(self):
        exc = Exception(
            "HTTPSConnectionPool: Max retries exceeded with url: "
            "/v2/fins/summary?code=14010 (Caused by ResponseError('too many 429'))"
        )
        self.assertFalse(jq._is_auth_failure(exc))
        self.assertTrue(jq._is_rate_limit(exc))

    def test_bulk_code_to_ticker(self):
        self.assertEqual(jq._bulk_code_to_ticker("72030"), "7203")
        self.assertEqual(jq._bulk_code_to_ticker("7203"), "7203")


if __name__ == "__main__":
    unittest.main()
