"""
committee.py / committee_prompts.py / notifier._committee_blocks のテスト

LLM 呼び出し（committee._invoke）と DB アクセスはすべてモックする。
"""

import json
import unittest
from unittest.mock import patch

# 読み込み時に実 config.json の値を束縛しないよう、既存テストと同じモック設定で import する
_IMPORT_CONFIG = {
    "slack": {"webhook_url": "https://XXXXX"},
    "jquants": {"earnings_alert_days_warning": "7", "earnings_alert_days_critical": "3"},
    "signal": {
        "pattern_a_rsi_min": "50", "pattern_a_rsi_max": "70",
        "pattern_b_rsi_min": "40", "pattern_b_rsi_max": "60",
    },
    "report_server": {"host": "127.0.0.1", "port": "5100"},
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
    "capital": {"total_capital_jpy": "1000000", "total_capital_usd": "10000",
                "max_risk_per_trade_pct": "2", "default_stop_loss_pct": "8"},
}

with patch("src.config.CONFIG", _IMPORT_CONFIG):
    from src import committee, committee_prompts, notifier

_KEYS = {
    "openai": {"api_key": "sk-test", "model": "gpt-4.1", "timeout_sec": 5},
    "anthropic": {"api_key": "sk-ant-test", "model": "claude", "timeout_sec": 5},
}


def _config(**committee_overrides) -> dict:
    return {
        "llm": _KEYS,
        "capital": {"max_concurrent_holdings": 3, "usdjpy_fallback": 150},
        "committee": {"enabled": True, **committee_overrides},
    }


def _entry_set(ticker="7203", score=80.0) -> dict:
    return {
        "ticker": ticker, "name": "トヨタ自動車", "market": "JP",
        "entry_price": 3200.0, "stop_loss_price": 2950.0, "stop_loss_pct": 7.8,
        "target_price": 3700.0, "final_score": score, "conditions_met": '["EMA25>EMA75"]',
    }


def _holding(ticker="AAPL", label="sell_watch") -> dict:
    return {
        "ticker": ticker, "name": "Apple", "market": "US", "label": label,
        "reasons": ["MA25割れ"], "pnl_pct": -3.0, "current_price": 180.0,
    }


def _analyst(stance="neutral") -> dict:
    return {"stance": stance, "confidence": 0.6, "key_points": ["p"], "risks": ["r"], "summary": "s"}


def _reply(obj) -> dict:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return {"text": text, "error": None, "prompt_tokens": 1000, "output_tokens": 100}


class _FakeLLM:
    """システムプロンプト・本文から役割を判定して応答する _invoke の代替。"""

    def __init__(self, stances=None, pm=None, fail_roles=(), bad_json_once=()):
        self.stances = stances or {}
        self.pm = pm or {"verdict": "BUY", "confidence": 0.7, "position_size_adj": "half",
                         "rationale": "根拠", "dissent": "少数意見"}
        self.fail_roles = set(fail_roles)
        self.bad_json_once = set(bad_json_once)
        self.calls: list[str] = []

    def _role(self, system: str, prompt: str) -> str:
        if "悪魔の代弁者として反論" in prompt:
            return "rebuttal"
        if "反論に 1 回だけ応答" in prompt:
            return "response"
        for role, name in committee_prompts.ROLE_NAMES.items():
            if name in system:
                return role
        raise AssertionError("role not found")

    def __call__(self, provider, system, prompt, cfg):
        role = self._role(system, prompt)
        self.calls.append(role)
        if role in self.fail_roles:
            return {"text": "", "error": "HTTP 500", "prompt_tokens": None, "output_tokens": None}
        if role in self.bad_json_once:
            self.bad_json_once.discard(role)
            return _reply("すみません、JSONではありません")
        if role == "rebuttal":
            targets = [r for r in ("fundamental", "technical") if self.stances.get(r) == "bullish"]
            return _reply({"rebuttals": [{"target": t, "point": "反論"} for t in targets], "summary": "下振れ"})
        if role == "response":
            return _reply({"response": "応答", "stance_after": "neutral"})
        if role == "pm":
            return _reply(self.pm)
        return _reply(_analyst(self.stances.get(role, "neutral")))


_MACRO = _analyst("neutral")
_INPUTS = {"fundamental": "f", "technical": "t", "risk": "r"}


class DeliberateTests(unittest.TestCase):

    def _run(self, fake, subject=None, macro=_MACRO, **cfg_overrides):
        subject = subject or {"subject_type": "buy_candidate", "item": _entry_set()}
        with patch.object(committee, "CONFIG", _config(**cfg_overrides)), \
             patch.object(committee, "_invoke", side_effect=fake), \
             patch.object(committee, "_build_analyst_inputs", return_value=_INPUTS):
            cfg = committee.get_committee_config()
            return committee.deliberate(subject, macro, [], [], cfg)

    def test_full_debate_with_rebuttal(self):
        """強気 2 名 → 反論 1 回 + 応答 2 回 + PM で full 判定"""
        fake = _FakeLLM(stances={"fundamental": "bullish", "technical": "bullish"})
        v = self._run(fake)
        self.assertEqual(v["status"], "full")
        self.assertEqual(v["verdict"], "BUY")
        self.assertEqual(sorted(fake.calls), sorted(
            ["fundamental", "technical", "risk", "rebuttal", "response", "response", "pm"]))
        self.assertEqual(set(v["agents"]["responses"]), {"fundamental", "technical"})
        self.assertEqual(v["agents"]["schema_version"], committee_prompts.SCHEMA_VERSION)
        self.assertGreater(v["cost_jpy"], 0)

    def test_no_bullish_skips_round2(self):
        """強気がいなければ反論・応答を呼ばない"""
        fake = _FakeLLM()
        v = self._run(fake)
        self.assertEqual(sorted(fake.calls), sorted(["fundamental", "technical", "risk", "pm"]))
        self.assertIsNone(v["agents"]["rebuttal"])
        self.assertEqual(v["status"], "full")

    def test_only_bullish_analyst_responds(self):
        """反論対象の強気アナリストだけが応答する"""
        fake = _FakeLLM(stances={"technical": "bullish"})
        v = self._run(fake)
        self.assertEqual(fake.calls.count("response"), 1)
        self.assertEqual(list(v["agents"]["responses"]), ["technical"])

    def test_absent_analyst_gives_partial(self):
        """1 名が失敗しても審議を続け partial になる"""
        fake = _FakeLLM(fail_roles={"fundamental"})
        v = self._run(fake)
        self.assertEqual(v["status"], "partial")
        self.assertIn("fundamental", v["absent"])
        self.assertIn("fundamental", v["agents"]["errors"])

    def test_quorum_not_met_skips_pm(self):
        """マクロ欠席 + 2 名失敗で定足数未満 → PM を呼ばず failed"""
        fake = _FakeLLM(fail_roles={"fundamental", "technical"})
        v = self._run(fake, macro=None)
        self.assertEqual(v["status"], "failed")
        self.assertNotIn("pm", fake.calls)
        self.assertIsNone(v["verdict"])

    def test_bad_json_is_retried_once(self):
        """JSON 解析に失敗したら 1 回だけ再要求する"""
        fake = _FakeLLM(bad_json_once={"technical"})
        v = self._run(fake)
        self.assertEqual(fake.calls.count("technical"), 2)
        self.assertEqual(v["status"], "full")

    def test_invalid_pm_verdict_fails(self):
        """PM が許可されていない判定語を返し続けたら failed"""
        fake = _FakeLLM(pm={"verdict": "STRONG_BUY", "confidence": 0.9})
        v = self._run(fake)
        self.assertEqual(v["status"], "failed")
        self.assertEqual(fake.calls.count("pm"), 2)

    def test_holding_uses_holding_verdicts(self):
        """保有銘柄は HOLD / REDUCE / SELL で判定する"""
        fake = _FakeLLM(pm={"verdict": "sell", "confidence": 1.4, "rationale": "x", "dissent": ""})
        v = self._run(fake, subject={"subject_type": "holding", "item": _holding()})
        self.assertEqual(v["verdict"], "SELL")
        self.assertEqual(v["confidence"], 1.0)

    def test_buy_verdict_rejected_for_holding(self):
        fake = _FakeLLM(pm={"verdict": "BUY"})
        v = self._run(fake, subject={"subject_type": "holding", "item": _holding()})
        self.assertEqual(v["status"], "failed")

    def test_disabled_agent_is_absent(self):
        """agents.<role>.enabled=false のエージェントは呼ばず欠席扱い"""
        fake = _FakeLLM()
        v = self._run(fake, agents={"risk": {"enabled": False}})
        self.assertNotIn("risk", fake.calls)
        self.assertIn("risk", v["absent"])


class RunCommitteeTests(unittest.TestCase):

    def _patches(self, cfg, **overrides):
        defaults = {
            "CONFIG": cfg,
            "_get_macro_brief": (_MACRO, committee._new_usage()),
            "_load_cached_verdict": None,
            "_spent_today": 0.0,
            "_save_verdict": None,
        }
        defaults.update(overrides)
        patches = []
        for name, value in defaults.items():
            if name == "CONFIG":
                patches.append(patch.object(committee, name, value))
            else:
                patches.append(patch.object(committee, name, return_value=value))
        return patches

    def _run(self, cfg, entry_sets, holdings, **overrides):
        patches = self._patches(cfg, **overrides)
        mocks = [p.start() for p in patches]
        self.addCleanup(lambda: [p.stop() for p in patches])
        return committee.run_committee(entry_sets, holdings, {"mode": "normal"}, "evening"), mocks

    def test_disabled_returns_none(self):
        result, _ = self._run({"committee": {"enabled": False}}, [_entry_set()], [])
        self.assertIsNone(result)

    def test_pm_without_key_returns_none(self):
        cfg = _config()
        cfg["llm"] = {"openai": _KEYS["openai"], "anthropic": {"api_key": "YOUR_ANTHROPIC_API_KEY"}}
        result, _ = self._run(cfg, [_entry_set()], [])
        self.assertIsNone(result)

    def test_no_subjects(self):
        result, _ = self._run(_config(), [], [_holding(label="hold")])
        self.assertEqual(result["verdicts"], [])

    def test_budget_exceeded_skips_without_llm(self):
        """日次予算到達時は審議せず skipped_budget を記録する"""
        with patch.object(committee, "deliberate") as mock_deliberate:
            result, _ = self._run(_config(daily_budget_jpy=10), [_entry_set()], [], _spent_today=10.0)
        mock_deliberate.assert_not_called()
        self.assertEqual(result["verdicts"][0]["status"], "skipped_budget")

    def test_cached_verdict_is_reused(self):
        cached = {"ticker": "7203", "status": "full", "verdict": "WAIT", "cost_jpy": 0.0}
        with patch.object(committee, "deliberate") as mock_deliberate:
            result, _ = self._run(_config(), [_entry_set()], [], _load_cached_verdict=cached)
        mock_deliberate.assert_not_called()
        self.assertEqual(result["verdicts"], [cached])

    def test_unexpected_error_becomes_failed(self):
        with patch.object(committee, "deliberate", side_effect=RuntimeError("boom")):
            result, _ = self._run(_config(), [_entry_set()], [])
        self.assertEqual(result["verdicts"][0]["status"], "failed")
        self.assertIn("boom", result["verdicts"][0]["agents"]["errors"]["committee"])


class SelectSubjectsTests(unittest.TestCase):

    def test_priority_and_limit(self):
        """損切り接近 → 売り検討 → 買い候補スコア降順、上限で切る"""
        holdings = [_holding("A", "sell_watch"), _holding("B", "hold"), _holding("C", "sl_near"),
                    _holding("D", "forced_stop")]
        entry_sets = [_entry_set("LOW", 50), _entry_set("HIGH", 90)]
        subjects = committee.select_subjects(entry_sets, holdings, 3)
        self.assertEqual([s["item"]["ticker"] for s in subjects], ["C", "A", "HIGH"])

    def test_forced_stop_and_hold_are_excluded(self):
        subjects = committee.select_subjects([], [_holding("D", "forced_stop"), _holding("B", "hold")], 5)
        self.assertEqual(subjects, [])


class HelperTests(unittest.TestCase):

    def test_parse_json_with_code_fence(self):
        self.assertEqual(committee.parse_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_parse_json_invalid(self):
        self.assertIsNone(committee.parse_json("JSONなし"))
        self.assertIsNone(committee.parse_json("{broken"))

    def test_estimate_cost_uses_model_price_or_default(self):
        with patch.object(committee, "CONFIG", _config(
            usdjpy_for_cost=100, pricing_usd_per_1m={"cheap": {"input": 1.0, "output": 2.0}},
        )):
            cfg = committee.get_committee_config()
        self.assertAlmostEqual(committee.estimate_cost_jpy("cheap", 1_000_000, 1_000_000, cfg), 300.0)
        # default: 3 + 15 USD
        self.assertAlmostEqual(committee.estimate_cost_jpy("unknown", 1_000_000, 1_000_000, cfg), 1800.0)


class PromptTests(unittest.TestCase):

    def test_holding_pm_prompt_lists_holding_verdicts(self):
        text = committee_prompts.build_pm_prompt("holding", "subject", {"risk": _analyst()}, None, {}, ["macro"])
        self.assertIn("HOLD | REDUCE | SELL", text)
        self.assertIn("マクロエコノミスト は応答できなかった", text)

    def test_format_subject_buy_candidate(self):
        text = committee_prompts.format_subject({"subject_type": "buy_candidate", "item": _entry_set()})
        self.assertIn("新規買い候補", text)
        self.assertIn("EMA25>EMA75", text)

    def test_format_fundamentals_without_data(self):
        text = committee_prompts.format_fundamentals({"data": {}}, None)
        self.assertIn("財務データなし", text)

    def test_format_fundamentals_marks_stale(self):
        text = committee_prompts.format_fundamentals(
            {"data": {"trailingPE": 12.3}, "fetched_date": "2026-09-20", "stale": True}, None)
        self.assertIn("PER(実績): 12.30", text)
        self.assertIn("過去データ", text)


class CommitteeBlocksTests(unittest.TestCase):

    def test_empty_returns_no_blocks(self):
        self.assertEqual(notifier._committee_blocks(None), [])
        self.assertEqual(notifier._committee_blocks({"verdicts": []}), [])

    def test_blocks_include_verdicts_and_disclaimer(self):
        committee_result = {
            "macro": {"stance": "bearish", "summary": "金利上昇"},
            "cost_jpy": 5.2,
            "verdicts": [
                {"ticker": "7203", "name": "トヨタ", "subject_type": "buy_candidate", "status": "partial",
                 "verdict": "BUY", "confidence": 0.8, "rationale": "根拠", "dissent": "反対意見",
                 "absent": ["fundamental"]},
                {"ticker": "AAPL", "name": "", "subject_type": "holding", "status": "skipped_budget"},
                {"ticker": "MSFT", "name": "", "subject_type": "holding", "status": "failed"},
            ],
        }
        blocks = notifier._committee_blocks(committee_result)
        self.assertEqual(len(blocks), 3)
        body = blocks[1]["text"]["text"]
        self.assertIn("🟢 *7203 トヨタ*（新規）→ *BUY*", body)
        self.assertIn("■■■■□ 80%", body)
        self.assertIn("欠席: ファンダ", body)
        self.assertIn("予算上限のため未審議", body)
        self.assertIn("審議不成立", body)
        self.assertIn("逆風", body)
        self.assertIn("投資助言ではありません", blocks[2]["text"]["text"])


if __name__ == "__main__":
    unittest.main()
