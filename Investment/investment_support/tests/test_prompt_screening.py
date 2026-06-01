"""Screening → Prompt 連携のテスト"""

import sys
from datetime import date
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

import sqlite3
import unittest

from src.prompt_screening import (
    enrich_tickers_from_daily_signals,
    merge_prompt_candidates,
)


def _init_db(conn):
    today = date.today().strftime("%Y-%m-%d")
    conn.executescript("""
        CREATE TABLE universe_master (
            ticker TEXT PRIMARY KEY, name TEXT, market TEXT
        );
        CREATE TABLE daily_signals (
            ticker TEXT, date TEXT, market TEXT, pattern TEXT, label TEXT,
            close REAL, rsi14 REAL, atr14 REAL, vol_ratio REAL,
            final_score REAL, recommend_rank TEXT,
            recommend_reason TEXT, market_mode TEXT,
            trend_score REAL, pattern_a_score REAL, pattern_b_score REAL,
            pattern_c_score REAL, liquidity_score REAL
        );
        CREATE TABLE technical_snapshot (
            ticker TEXT, date TEXT, ma5 REAL, ma25 REAL, ma75 REAL, ma200 REAL,
            rsi14 REAL, vol_20d_avg REAL, high_52w REAL
        );
        CREATE TABLE price_history (
            ticker TEXT, date TEXT, adj_close REAL, volume REAL, is_holiday INTEGER
        );
        CREATE TABLE holdings (id INTEGER PRIMARY KEY, is_closed INTEGER);
        CREATE TABLE earnings_schedule (ticker TEXT, announce_date TEXT);
    """)
    conn.execute(
        "INSERT INTO universe_master VALUES ('7203', 'トヨタ', 'JP')",
    )
    conn.execute(
        "INSERT INTO daily_signals VALUES "
        "('7203', ?, 'JP', 'A', 'watch', 3000, 55, 80, 1.2, 72, 'B', 'テスト', 'normal', "
        "70, 60, 50, 10, 85)",
        (today,),
    )
    conn.execute(
        "INSERT INTO technical_snapshot VALUES "
        "('7203', ?, 3050, 2900, 2800, 2600, 55, 1200000, 3100)",
        (today,),
    )
    conn.execute(
        "INSERT INTO price_history VALUES ('7203', ?, 3000, 1500000, 0)",
        (today,),
    )
    conn.commit()


class PromptScreeningTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        _init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_enrich_from_daily_signals(self):
        rows = enrich_tickers_from_daily_signals(self.conn, ["7203"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticker"], "7203")
        self.assertTrue(rows[0].get("_from_screening"))
        self.assertEqual(rows[0].get("ds_label"), "watch")
        self.assertIsNotNone(rows[0].get("entry_price"))

    def test_merge_prefers_screening_when_requested(self):
        log = [{"ticker": "7203", "final_score": 50, "entry_price": 1000}]
        screening = [{"ticker": "7203", "final_score": 72, "_from_screening": True, "entry_price": 3000}]
        merged = merge_prompt_candidates(log, screening, prefer_screening=True)
        self.assertEqual(merged[0]["entry_price"], 3000)


if __name__ == "__main__":
    unittest.main()
