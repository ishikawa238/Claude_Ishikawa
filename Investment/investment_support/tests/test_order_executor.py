import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src import order_executor


class _FakeFrame:
    def __init__(self, rows):
        self._rows = rows

    def __len__(self):
        return len(self._rows)

    def to_dict(self, orient):
        if orient != "records":
            raise AssertionError(f"unexpected orient: {orient}")
        return self._rows


class _FakeTradeContext:
    def __init__(self, unlock_result, place_result, **kwargs):
        self.unlock_result = unlock_result
        self.place_result = place_result
        self.kwargs = kwargs
        self.unlock_called = False
        self.closed = False

    def unlock_trade(self, password_md5=None):
        self.unlock_called = True
        return self.unlock_result

    def place_order(self, **kwargs):
        return self.place_result

    def close(self):
        self.closed = True


class PlaceOrderTests(unittest.TestCase):
    def _build_fake_ft(self, unlock_result, place_result):
        holder = {}

        def factory(**kwargs):
            ctx = _FakeTradeContext(unlock_result, place_result, **kwargs)
            holder["ctx"] = ctx
            return ctx

        fake_ft = SimpleNamespace(
            TrdSide=SimpleNamespace(BUY="BUY", SELL="SELL"),
            TrdEnv=SimpleNamespace(REAL="REAL", SIMULATE="SIMULATE"),
            TrdMarket=SimpleNamespace(US="US", JP="JP"),
            SubAccType=SimpleNamespace(JP_TOKUTEI="JP_TOKUTEI"),
            SecurityFirm=SimpleNamespace(FUTUSECURITIES="FUTUSECURITIES", FUTUJP="FUTUJP"),
            OrderType=SimpleNamespace(MARKET="MARKET"),
            OpenSecTradeContext=factory,
            RET_OK=0,
            RET_ERROR=-1,
        )
        return fake_ft, holder

    def test_place_order_continues_when_gui_unlock_api_is_disabled(self):
        unlock_result = (
            -1,
            "Please use the unlock button in the interface to unlock trading, as the unlock API has been disabled in the GUI version.",
        )
        place_result = (0, _FakeFrame([{"order_id": "12345"}]))
        fake_ft, holder = self._build_fake_ft(unlock_result, place_result)

        with patch.dict(sys.modules, {"moomoo": fake_ft}):
            with patch.object(order_executor, "_TRADE_PWD_MD5", "dummy-md5"):
                result = order_executor.place_order("sell", "7203", "JP", 100)

        self.assertTrue(result["success"])
        self.assertEqual(result["order_id"], "12345")
        self.assertTrue(holder["ctx"].unlock_called)
        self.assertTrue(holder["ctx"].closed)

    def test_place_order_returns_manual_unlock_guidance_when_still_locked(self):
        unlock_result = (
            -1,
            "Please use the unlock button in the interface to unlock trading, as the unlock API has been disabled in the GUI version.",
        )
        place_result = (-1, "Trading is locked. Please unlock trading first.")
        fake_ft, _ = self._build_fake_ft(unlock_result, place_result)

        with patch.dict(sys.modules, {"moomoo": fake_ft}):
            with patch.object(order_executor, "_TRADE_PWD_MD5", "dummy-md5"):
                result = order_executor.place_order("sell", "7203", "JP", 100)

        self.assertFalse(result["success"])
        self.assertIn("GUI版 OpenD", result["message"])
        self.assertIn("取引ロックを解除", result["message"])


if __name__ == "__main__":
    unittest.main()