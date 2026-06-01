"""prompt_builder モジュールのテスト"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

import unittest

from src.prompt_builder import _build_buy_prompt, _pick_items_by_tickers, generate_prompt


def _candidate(ticker: str, name: str = "") -> dict:
    return {
        "ticker": ticker,
        "name": name or ticker,
        "pattern": "A",
        "entry_price": 1000,
        "stop_loss_price": 900,
        "stop_loss_pct": 10,
        "target_price": 1200,
        "recommended_amount": 50000,
        "final_score": 75,
        "recommend_rank": "A",
    }


class PromptBuilderBuyMultiTestCase(unittest.TestCase):
    def test_pick_items_preserves_ticker_order(self):
        items = [_candidate("B"), _candidate("A"), _candidate("C")]
        picked, truncated = _pick_items_by_tickers(items, ["C", "A"], max_items=10)
        self.assertEqual([p["ticker"] for p in picked], ["C", "A"])
        self.assertFalse(truncated)

    def test_buy_prompt_includes_multiple_candidates(self):
        candidates = [_candidate("7203", "トヨタ"), _candidate("9984", "SBG")]
        prompt = _build_buy_prompt(
            ["7203", "9984"],
            [],
            candidates,
            {"mode": "normal"},
            {"beginner_glossary": False},
        )
        self.assertIn("7203", prompt)
        self.assertIn("9984", prompt)
        self.assertIn("2 銘柄", prompt)
        self.assertIn("優先順位", prompt)

    def test_buy_prompt_single_uses_single_question(self):
        candidates = [_candidate("7203")]
        prompt = _build_buy_prompt(
            ["7203"],
            [],
            candidates,
            {"mode": "normal"},
            {"beginner_glossary": False},
        )
        self.assertIn("7203", prompt)
        self.assertNotIn("優先順位と資金配分", prompt)

    def test_generate_prompt_buy_multi(self):
        result = generate_prompt(
            "buy",
            ["A", "B"],
            [],
            [_candidate("A"), _candidate("B")],
            {"mode": "normal"},
            {},
        )
        self.assertGreater(result["estimated_tokens"], 0)
        self.assertIn("本日の買い候補（2件）", result["prompt"])


if __name__ == "__main__":
    unittest.main()
