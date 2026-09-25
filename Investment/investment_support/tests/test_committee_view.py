"""committee_view（ダッシュボード用議事録整形）のテスト。"""

import json
import sqlite3
import unittest
from unittest.mock import patch

_CONFIG_MOCK = {
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
    "logging": {"log_dir": "/tmp", "log_level": "INFO"},
}


def _build_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE committee_verdict_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            verdict_date DATE NOT NULL,
            batch_type TEXT NOT NULL,
            ticker TEXT NOT NULL,
            market TEXT,
            subject_type TEXT NOT NULL,
            verdict TEXT,
            confidence REAL,
            position_size_adj TEXT,
            pm_rationale TEXT,
            dissent TEXT,
            agents_json TEXT,
            prompt_tokens INTEGER,
            output_tokens INTEGER,
            estimated_cost_jpy REAL,
            latency_ms INTEGER,
            status TEXT,
            created_at TEXT
        );
        CREATE TABLE committee_macro_brief (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            brief_date DATE NOT NULL,
            batch_type TEXT NOT NULL,
            brief_json TEXT,
            prompt_tokens INTEGER,
            output_tokens INTEGER,
            estimated_cost_jpy REAL,
            created_at TEXT
        );
        """
    )
    return conn


_AGENTS = {
    "schema_version": 1,
    "round1": {
        "macro": {
            "stance": "bullish", "confidence": 0.8,
            "summary": "マクロ要約", "key_points": ["点1"], "risks": ["リ1"],
        },
        "fundamental": {
            "stance": "neutral", "confidence": 0.5,
            "summary": "ファンダ要約", "key_points": [], "risks": ["負債"],
        },
        "technical": {
            "stance": "bullish", "confidence": 0.7,
            "summary": "テクニカル要約", "key_points": ["出来高"], "risks": [],
        },
        "risk": {
            "stance": "bearish", "confidence": 0.6,
            "summary": "リスク要約", "key_points": [], "risks": ["ボラ"],
        },
    },
    "rebuttal": {
        "rebuttals": [{"target": "technical", "point": "過熱感がある"}],
    },
    "responses": {
        "technical": {"response": "一部同意する", "stance_after": "neutral"},
    },
    "pm": {
        "verdict": "WAIT", "confidence": 0.55, "position_size_adj": "skip",
        "rationale": "根拠", "dissent": "テクニカルは強気",
    },
    "absent": [],
    "errors": {},
}


with patch("src.config.CONFIG", _CONFIG_MOCK):
    from src import committee_view as cv


class CommitteeViewTests(unittest.TestCase):

    def setUp(self):
        self.conn = _build_db()
        self.conn.execute(
            "INSERT INTO committee_macro_brief "
            "(brief_date, batch_type, brief_json, estimated_cost_jpy, prompt_tokens, output_tokens) "
            "VALUES ('2026-09-24', 'evening', ?, 0.2, 10, 20)",
            (json.dumps({
                "jp": {"stance": "bullish", "confidence": 0.8, "summary": "日経好調",
                       "key_points": ["上昇"], "risks": []},
                "us": {"stance": "neutral", "confidence": 0.5, "summary": "様子見",
                       "key_points": [], "risks": ["金利"]},
            }, ensure_ascii=False),),
        )
        self.conn.execute(
            "INSERT INTO committee_verdict_log "
            "(verdict_date, batch_type, ticker, market, subject_type, verdict, confidence, "
            "position_size_adj, pm_rationale, dissent, agents_json, prompt_tokens, output_tokens, "
            "estimated_cost_jpy, latency_ms, status, created_at) "
            "VALUES ('2026-09-24', 'evening', '8923', 'JP', 'buy_candidate', 'WAIT', 0.55, "
            "'skip', '根拠', '少数', ?, 100, 50, 1.5, 12000, 'full', '2026-09-24 16:41:00')",
            (json.dumps(_AGENTS, ensure_ascii=False),),
        )
        self.conn.commit()

        def fake_get_connection():
            class _CM:
                def __enter__(_self):
                    return self.conn
                def __exit__(_self, *a):
                    return False
            return _CM()

        self._patcher = patch.object(cv, "get_connection", fake_get_connection)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def test_list_and_latest_dates(self):
        self.assertEqual(cv.list_verdict_dates(), ["2026-09-24"])
        self.assertEqual(cv.latest_verdict_date(), "2026-09-24")

    def test_load_day_includes_macro_and_round_debate(self):
        day = cv.load_day("2026-09-24")
        self.assertEqual(day["date"], "2026-09-24")
        self.assertAlmostEqual(day["cost_jpy"], 1.7)
        self.assertEqual(len(day["macros"]), 1)
        self.assertEqual(day["macros"][0]["jp"]["stance"], "bullish")
        self.assertEqual(day["macros"][0]["us"]["stance_label"], "中立")

        v = day["verdicts"][0]
        self.assertEqual(v["ticker"], "8923")
        self.assertEqual(v["verdict"], "WAIT")
        self.assertEqual(v["subject_label"], "新規買い候補")
        roles = [op["role"] for op in v["round1"]]
        self.assertEqual(roles, ["macro", "fundamental", "technical", "risk"])
        self.assertEqual(v["round1"][2]["stance_label"], "強気")
        self.assertEqual(v["rebuttal"][0]["target"], "technical")
        self.assertEqual(v["responses"][0]["stance_after"], "neutral")
        self.assertIn("過熱感", v["rebuttal"][0]["point"])

    def test_load_verdict_by_id(self):
        detail = cv.load_verdict(1)
        self.assertIsNotNone(detail)
        self.assertEqual(detail["id"], 1)
        self.assertEqual(detail["rationale"], "根拠")
        self.assertIsNone(cv.load_verdict(999))

    def test_absent_role_is_marked(self):
        agents = dict(_AGENTS)
        agents["round1"] = {"fundamental": _AGENTS["round1"]["fundamental"]}
        agents["absent"] = ["macro", "technical", "risk"]
        self.conn.execute(
            "INSERT INTO committee_verdict_log "
            "(verdict_date, batch_type, ticker, market, subject_type, verdict, status, agents_json) "
            "VALUES ('2026-09-23', 'morning', 'AAPL', 'US', 'holding', NULL, 'failed', ?)",
            (json.dumps(agents, ensure_ascii=False),),
        )
        self.conn.commit()
        day = cv.load_day("2026-09-23")
        absent_roles = {op["role"] for op in day["verdicts"][0]["round1"] if op.get("absent")}
        self.assertEqual(absent_roles, {"macro", "technical", "risk"})


if __name__ == "__main__":
    unittest.main()
