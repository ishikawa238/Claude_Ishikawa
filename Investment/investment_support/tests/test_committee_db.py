"""
committee.py の DB 操作（保存・予算集計・キャッシュ）と fundamentals_fetcher のキャッシュを、
init_db.py の DDL で作った一時 DB で検証する。
"""

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest.mock import patch

from shared.db import get_connection as shared_get_connection

# 読み込み時に実 config.json の値を束縛しないよう、モック設定で import する
_IMPORT_CONFIG = {
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
    "capital": {"total_capital_jpy": "1000000", "total_capital_usd": "10000",
                "max_risk_per_trade_pct": "2", "default_stop_loss_pct": "8"},
}

with patch("src.config.CONFIG", _IMPORT_CONFIG):
    from scripts.init_db import DDL_STATEMENTS
    from src import committee, fundamentals_fetcher

_TABLES = ("committee_verdict_log", "committee_macro_brief", "fundamentals_cache")


class CommitteeDbTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        conn = sqlite3.connect(self.db_path)
        for ddl in DDL_STATEMENTS:
            if any(t in ddl for t in _TABLES):
                conn.execute(ddl)
        conn.commit()
        conn.close()

        @contextmanager
        def _conn():
            with shared_get_connection(self.db_path) as c:
                yield c

        patches = [
            patch.object(committee, "get_connection", _conn),
            patch.object(fundamentals_fetcher, "get_connection", _conn),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    def _verdict(self, status="full", cost=4.5) -> dict:
        v = committee._empty_verdict(
            {"subject_type": "buy_candidate", "item": {"ticker": "7203", "market": "JP"}}, status)
        v.update({"verdict": "BUY", "confidence": 0.7, "cost_jpy": cost, "rationale": "根拠"})
        v["agents"] = {"schema_version": 1, "absent": []}
        return v

    def test_save_then_spent_and_cache(self):
        committee._save_verdict(self._verdict(), "2026-09-24", "evening")
        with committee.get_connection() as conn:
            conn.execute(
                "INSERT INTO committee_macro_brief (brief_date, batch_type, brief_json, estimated_cost_jpy) "
                "VALUES ('2026-09-24', 'evening', '{}', 0.5)"
            )
        self.assertAlmostEqual(committee._spent_today("2026-09-24"), 5.0)
        self.assertAlmostEqual(committee._spent_today("2026-09-25"), 0.0)

        cached = committee._load_cached_verdict("2026-09-24", "evening", "7203", "buy_candidate")
        self.assertEqual(cached["verdict"], "BUY")
        self.assertTrue(cached["cached"])
        self.assertIsNone(committee._load_cached_verdict("2026-09-24", "morning", "7203", "buy_candidate"))

    def test_skipped_verdict_is_not_cached(self):
        committee._save_verdict(self._verdict(status="skipped_budget", cost=0), "2026-09-24", "evening")
        self.assertIsNone(committee._load_cached_verdict("2026-09-24", "evening", "7203", "buy_candidate"))

    def test_fundamentals_cache_and_stale_fallback(self):
        with patch.object(fundamentals_fetcher, "_fetch_info_yf",
                          return_value={"trailingPE": 10.0, "unused": 1}) as mock_fetch:
            first = fundamentals_fetcher.get_fundamentals("7203", "JP", date(2026, 9, 23))
            again = fundamentals_fetcher.get_fundamentals("7203", "JP", date(2026, 9, 23))
        self.assertEqual(first["data"], {"trailingPE": 10.0})
        self.assertEqual(again["data"], {"trailingPE": 10.0})
        mock_fetch.assert_called_once_with("7203.T")

        with patch.object(fundamentals_fetcher, "_fetch_info_yf", side_effect=RuntimeError("down")):
            stale = fundamentals_fetcher.get_fundamentals("7203", "JP", date(2026, 9, 24))
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["fetched_date"], "2026-09-23")


if __name__ == "__main__":
    unittest.main()
