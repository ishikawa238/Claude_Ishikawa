"""
order_server のテスト

テスト対象:
- /health → 200 OK
- /report → 200 + サマリーカード・テーブル等の HTML 要素
- /stock/<ticker> → 200 (存在する銘柄) / 404 (存在しない銘柄)
- /performance → 200 + サマリー統計の HTML 要素
- /api/portfolio-pnl → JSON 構造
- /api/score-radar → パラメータ検証
- /api/price-history → JSON 構造
- /api/market-timeline → JSON 構造
- /api/perf-monthly → JSON 構造
- /api/perf-cumulative → JSON 構造
- /api/sector-alloc → JSON 構造
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent  # Ishikawa238/
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

import json
import sqlite3
import unittest
from contextlib import contextmanager
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

# CONFIG モック
_CONFIG_MOCK = {
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
    "logging": {"log_dir": "/tmp", "log_level": "INFO"},
    "report_server": {"host": "127.0.0.1", "port": "5100"},
    "risk": {"risk_free_rate_jp": "0.001", "risk_free_rate_us": "0.05"},
}

# テスト用 DB 接続を共有する仕組み
_test_conn = None


@contextmanager
def _get_test_connection():
    """テスト用のコンテキストマネージャ。共有 DB 接続を返す。"""
    yield _test_conn


import src.config as _src_config
_src_config.CONFIG = _CONFIG_MOCK

import scripts.order_server as _server_mod
from scripts.order_server import app


def _init_test_db(conn):
    """テスト用のテーブルとデータを作成する。"""
    conn.execute("PRAGMA foreign_keys = OFF")

    # テーブル作成（最小限）
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS universe_master (
            id INTEGER PRIMARY KEY, ticker TEXT UNIQUE, name TEXT, market TEXT,
            exchange TEXT, shares_outstanding REAL, is_active INTEGER DEFAULT 1,
            is_excluded INTEGER DEFAULT 0, exclude_reason TEXT,
            last_updated DATE, created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            sector TEXT, industry TEXT
        );
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY, ticker TEXT, date DATE, open REAL, high REAL,
            low REAL, close REAL, adj_close REAL, volume REAL,
            is_holiday INTEGER DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ph_td ON price_history(ticker, date);
        CREATE TABLE IF NOT EXISTS technical_snapshot (
            id INTEGER PRIMARY KEY, ticker TEXT, date DATE,
            ma5 REAL, ma25 REAL, ma75 REAL, ma200 REAL,
            ma5_5d_ago REAL, ma25_5d_ago REAL, ma200_5d_ago REAL,
            vol_20d_avg REAL, high_20d REAL, high_52w REAL, low_52w REAL,
            return_5d REAL, return_20d REAL, rsi14 REAL, atr14 REAL,
            volatility_20d REAL, est_turnover REAL, est_market_cap REAL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ts_td ON technical_snapshot(ticker, date);
        CREATE TABLE IF NOT EXISTS index_snapshot (
            id INTEGER PRIMARY KEY, index_code TEXT, date DATE, close REAL,
            ma25 REAL, ma75 REAL, ma200 REAL, ma25_5d_ago REAL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_is_cd ON index_snapshot(index_code, date);
        CREATE TABLE IF NOT EXISTS market_environment (
            id INTEGER PRIMARY KEY, date DATE UNIQUE, jp_above_ma25 INTEGER,
            us_above_ma25 INTEGER, mode TEXT, n225_close REAL, n225_ma25 REAL,
            sp500_close REAL, sp500_ma25 REAL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS holdings (
            id INTEGER PRIMARY KEY, ticker TEXT, name TEXT, market TEXT,
            entry_price REAL, entry_date DATE, quantity REAL,
            stop_loss_price REAL, memo TEXT, is_closed INTEGER DEFAULT 0,
            close_price REAL, close_date DATE, close_reason TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS daily_signals (
            id INTEGER PRIMARY KEY, ticker TEXT, date DATE, market TEXT,
            pattern TEXT, label TEXT,
            cond_a_ma200_rising INTEGER, cond_a_ma_aligned INTEGER,
            cond_a_52w_high_near INTEGER, cond_a_volume_surge INTEGER,
            cond_a_rsi_ok INTEGER, cond_a_above_ma25 INTEGER,
            cond_b_ma200_rising INTEGER, cond_b_ma_aligned INTEGER,
            cond_b_pullback_touched INTEGER, cond_b_bounced INTEGER,
            cond_b_ma25_rising INTEGER, cond_b_rsi_ok INTEGER,
            close REAL, rsi14 REAL, atr14 REAL, vol_ratio REAL, rs_rank INTEGER,
            trend_score REAL, pattern_a_score REAL, pattern_b_score REAL,
            liquidity_score REAL, base_score REAL, market_multiplier REAL,
            final_score REAL, recommend_rank TEXT, recommend_reason TEXT,
            market_mode TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ds_td ON daily_signals(ticker, date);
        CREATE TABLE IF NOT EXISTS trade_signal_log (
            id INTEGER PRIMARY KEY, signal_date DATE, ticker TEXT, name TEXT,
            market TEXT, pattern TEXT, entry_price REAL, stop_loss_price REAL,
            stop_loss_pct REAL, target_price REAL, recommended_amount REAL,
            market_mode TEXT, conditions_met TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS trade_result_log (
            id INTEGER PRIMARY KEY, signal_id INTEGER, ticker TEXT,
            actual_entry_price REAL, actual_entry_date DATE,
            actual_exit_price REAL, actual_exit_date DATE,
            exit_reason TEXT, pnl_pct REAL, pnl_amount REAL, memo TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS fundamentals_snapshot (
            id INTEGER PRIMARY KEY, ticker TEXT, fiscal_year TEXT,
            fiscal_quarter TEXT, revenue REAL, operating_income REAL,
            ordinary_income REAL, net_income REAL, eps REAL, bps REAL,
            shares_outstanding REAL, dividend_per_share REAL,
            per REAL, pbr REAL, dividend_yield REAL,
            fetched_at DATE, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS risk_snapshot (
            ticker TEXT NOT NULL, calc_date TEXT NOT NULL,
            var_99_1d REAL, beta_60d REAL, sharpe_60d REAL,
            updated_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (ticker, calc_date)
        );
    """)

    today = date.today()
    today_str = today.strftime("%Y-%m-%d")
    yesterday_str = (today - timedelta(days=1)).strftime("%Y-%m-%d")

    # 銘柄マスタ
    conn.execute(
        "INSERT INTO universe_master (ticker, name, market, last_updated, sector, industry) "
        "VALUES ('7203', 'トヨタ自動車', 'JP', ?, '輸送用機器', '自動車')",
        (today_str,),
    )
    conn.execute(
        "INSERT INTO universe_master (ticker, name, market, last_updated, sector, industry) "
        "VALUES ('AAPL', 'Apple Inc', 'US', ?, 'Technology', 'Consumer Electronics')",
        (today_str,),
    )

    # 価格履歴（60日分生成）
    for i in range(65):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        base_jp = 3000 + i * 5
        base_us = 180 + i * 0.5
        conn.execute(
            "INSERT OR IGNORE INTO price_history (ticker, date, open, high, low, close, adj_close, volume, is_holiday) "
            "VALUES ('7203', ?, ?, ?, ?, ?, ?, 1500000, 0)",
            (d, base_jp - 20, base_jp + 30, base_jp - 30, base_jp, base_jp),
        )
        conn.execute(
            "INSERT OR IGNORE INTO price_history (ticker, date, open, high, low, close, adj_close, volume, is_holiday) "
            "VALUES ('AAPL', ?, ?, ?, ?, ?, ?, 50000000, 0)",
            (d, base_us - 2, base_us + 3, base_us - 3, base_us, base_us),
        )

    # インデックススナップショット（60日分）
    for i in range(65):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        conn.execute(
            "INSERT OR IGNORE INTO index_snapshot (index_code, date, close, ma25) "
            "VALUES ('N225', ?, ?, ?)",
            (d, 38000 + i * 50, 37500 + i * 40),
        )
        conn.execute(
            "INSERT OR IGNORE INTO index_snapshot (index_code, date, close, ma25) "
            "VALUES ('SP500', ?, ?, ?)",
            (d, 5200 + i * 10, 5100 + i * 8),
        )

    # テクニカルスナップショット
    conn.execute(
        "INSERT INTO technical_snapshot (ticker, date, ma5, ma25, ma75, ma200, rsi14, atr14, vol_20d_avg) "
        "VALUES ('7203', ?, 3050, 2900, 2800, 2600, 58.5, 85.0, 1200000)",
        (today_str,),
    )
    conn.execute(
        "INSERT INTO technical_snapshot (ticker, date, ma5, ma25, ma75, ma200, rsi14, atr14, vol_20d_avg) "
        "VALUES ('AAPL', ?, 182, 178, 170, 160, 62.3, 3.5, 55000000)",
        (today_str,),
    )

    # 市場環境
    conn.execute(
        "INSERT INTO market_environment (date, mode, n225_close, n225_ma25, sp500_close, sp500_ma25, jp_above_ma25, us_above_ma25) "
        "VALUES (?, 'normal', 38500, 37800, 5300, 5150, 1, 1)",
        (today_str,),
    )
    conn.execute(
        "INSERT INTO market_environment (date, mode, n225_close, n225_ma25, sp500_close, sp500_ma25, jp_above_ma25, us_above_ma25) "
        "VALUES (?, 'caution', 37000, 37500, 5100, 5150, 0, 0)",
        (yesterday_str,),
    )

    # 保有銘柄
    conn.execute(
        "INSERT INTO holdings (ticker, name, market, entry_price, entry_date, quantity, stop_loss_price, is_closed) "
        "VALUES ('7203', 'トヨタ自動車', 'JP', 2800, ?, 100, 2650, 0)",
        ((today - timedelta(days=10)).strftime("%Y-%m-%d"),),
    )
    conn.execute(
        "INSERT INTO holdings (ticker, name, market, entry_price, entry_date, quantity, stop_loss_price, is_closed) "
        "VALUES ('AAPL', 'Apple Inc', 'US', 170, ?, 50, 160, 0)",
        ((today - timedelta(days=5)).strftime("%Y-%m-%d"),),
    )

    # daily_signals（スコア付き）
    conn.execute(
        "INSERT INTO daily_signals (ticker, date, market, pattern, label, "
        "trend_score, pattern_a_score, pattern_b_score, liquidity_score, "
        "final_score, recommend_rank, recommend_reason, market_mode) "
        "VALUES ('7203', ?, 'JP', 'A', 'candidate', 80, 65, 50, 90, 78, 'A', "
        "'上昇トレンドが継続中。52週高値に接近。流動性が高い', 'normal')",
        (today_str,),
    )
    conn.execute(
        "INSERT INTO daily_signals (ticker, date, market, pattern, label, "
        "trend_score, pattern_a_score, pattern_b_score, liquidity_score, "
        "final_score, recommend_rank, recommend_reason, market_mode) "
        "VALUES ('AAPL', ?, 'US', 'B', 'candidate', 70, 55, 75, 85, 72, 'B', "
        "'中期トレンドは良好。押し目反発の形成を確認', 'normal')",
        (today_str,),
    )

    # シグナルログ
    conn.execute(
        "INSERT INTO trade_signal_log (signal_date, ticker, name, market, pattern, "
        "entry_price, stop_loss_price, stop_loss_pct, target_price, recommended_amount, market_mode) "
        "VALUES (?, '7203', 'トヨタ自動車', 'JP', 'A', 3000, 2850, 5.0, 3300, 500000, 'normal')",
        (today_str,),
    )

    # トレード結果
    conn.execute(
        "INSERT INTO trade_result_log (signal_id, ticker, actual_entry_price, actual_entry_date, "
        "actual_exit_price, actual_exit_date, exit_reason, pnl_pct, pnl_amount) "
        "VALUES (1, '7203', 2800, ?, 3000, ?, 'take_profit', 7.14, 20000)",
        ((today - timedelta(days=30)).strftime("%Y-%m-%d"), (today - timedelta(days=5)).strftime("%Y-%m-%d")),
    )

    # ファンダメンタルズ
    conn.execute(
        "INSERT INTO fundamentals_snapshot (ticker, fiscal_year, fiscal_quarter, "
        "eps, bps, per, pbr, dividend_yield, fetched_at) "
        "VALUES ('7203', '2024', 'Q4', 250.5, 3200.0, 12.5, 1.1, 2.8, ?)",
        (today_str,),
    )

    conn.commit()


class OrderServerTestCase(unittest.TestCase):
    """order_server 全エンドポイントのテスト"""

    @classmethod
    def setUpClass(cls):
        global _test_conn
        _test_conn = sqlite3.connect(":memory:")
        _test_conn.row_factory = sqlite3.Row
        _init_test_db(_test_conn)

        # get_connection をテスト用に差し替え
        cls._patches = [
            patch.object(_server_mod, "get_connection", _get_test_connection),
        ]
        for p in cls._patches:
            p.start()

        app.config["TESTING"] = True
        cls.client = app.test_client()

    @classmethod
    def tearDownClass(cls):
        for p in cls._patches:
            p.stop()
        _test_conn.close()

    # ---- /health ----

    def test_health_returns_200(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.decode(), "OK")

    # ---- /report ----

    def test_report_returns_200(self):
        resp = self.client.get("/report")
        self.assertEqual(resp.status_code, 200)

    def test_report_contains_summary_cards(self):
        resp = self.client.get("/report")
        html = resp.data.decode()
        self.assertIn("資産総額", html)
        self.assertIn("損益合計", html)
        self.assertIn("市場環境", html)
        self.assertIn("保有銘柄数", html)
        self.assertIn("直近7日シグナル", html)

    def test_report_contains_holdings_table(self):
        resp = self.client.get("/report")
        html = resp.data.decode()
        self.assertIn("7203", html)
        self.assertIn("AAPL", html)
        self.assertIn("RSI", html)
        self.assertIn("ATR", html)
        self.assertIn("Score", html)
        self.assertIn("Rank", html)
        self.assertIn("Trend", html)

    def test_report_contains_chart_elements(self):
        resp = self.client.get("/report")
        html = resp.data.decode()
        self.assertIn("chart.js", html.lower())
        self.assertIn("pnlChart", html)
        self.assertIn("radarChart", html)
        self.assertIn("sectorChart", html)

    def test_report_contains_market_timeline(self):
        resp = self.client.get("/report")
        html = resp.data.decode()
        self.assertIn("n225Chart", html)
        self.assertIn("sp500Chart", html)
        self.assertIn("市場環境タイムライン", html)

    def test_report_ticker_links_to_stock_page(self):
        resp = self.client.get("/report")
        html = resp.data.decode()
        self.assertIn('href="/stock/7203"', html)
        self.assertIn('href="/stock/AAPL"', html)

    # ---- /stock/<ticker> ----

    def test_stock_existing_returns_200(self):
        resp = self.client.get("/stock/7203")
        self.assertEqual(resp.status_code, 200)

    def test_stock_nonexistent_returns_404(self):
        resp = self.client.get("/stock/ZZZZ")
        self.assertEqual(resp.status_code, 404)

    def test_stock_contains_technicals(self):
        resp = self.client.get("/stock/7203")
        html = resp.data.decode()
        self.assertIn("テクニカル指標", html)
        self.assertIn("RSI", html)
        self.assertIn("ATR", html)
        self.assertIn("MA25", html)

    def test_stock_contains_fundamentals(self):
        resp = self.client.get("/stock/7203")
        html = resp.data.decode()
        self.assertIn("ファンダメンタルズ", html)
        self.assertIn("PER", html)
        self.assertIn("PBR", html)

    def test_stock_contains_risk_metrics(self):
        resp = self.client.get("/stock/7203")
        html = resp.data.decode()
        self.assertIn("リスク指標", html)
        self.assertIn("VaR", html)
        self.assertIn("ベータ値", html)
        self.assertIn("シャープレシオ", html)

    def test_stock_contains_score_radar(self):
        resp = self.client.get("/stock/7203")
        html = resp.data.decode()
        self.assertIn("stockRadar", html)
        self.assertIn("score-radar", html)

    def test_stock_contains_signal_history(self):
        resp = self.client.get("/stock/7203")
        html = resp.data.decode()
        self.assertIn("シグナル履歴", html)
        self.assertIn("モメンタムブレイクアウト", html)

    def test_stock_us_ticker(self):
        resp = self.client.get("/stock/AAPL")
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn("Apple", html)
        self.assertIn("Technology", html)

    # ---- /performance ----

    def test_performance_returns_200(self):
        resp = self.client.get("/performance")
        self.assertEqual(resp.status_code, 200)

    def test_performance_contains_summary_stats(self):
        resp = self.client.get("/performance")
        html = resp.data.decode()
        self.assertIn("勝率", html)
        self.assertIn("平均損益", html)
        self.assertIn("PF", html)

    def test_performance_contains_charts(self):
        resp = self.client.get("/performance")
        html = resp.data.decode()
        self.assertIn("cumChart", html)
        self.assertIn("monthlyChart", html)

    def test_performance_contains_pattern_stats(self):
        resp = self.client.get("/performance")
        html = resp.data.decode()
        self.assertIn("パターン別成績", html)

    def test_performance_contains_trade_list(self):
        resp = self.client.get("/performance")
        html = resp.data.decode()
        self.assertIn("直近トレード", html)

    # ---- /api/portfolio-pnl ----

    def test_api_portfolio_pnl_returns_json(self):
        resp = self.client.get("/api/portfolio-pnl")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("dates", data)
        self.assertIn("pnl", data)
        self.assertIn("pnl_pct", data)
        self.assertIsInstance(data["dates"], list)
        self.assertIsInstance(data["pnl"], list)

    # ---- /api/score-radar ----

    def test_api_score_radar_missing_ticker_returns_400(self):
        resp = self.client.get("/api/score-radar")
        self.assertEqual(resp.status_code, 400)

    def test_api_score_radar_valid_ticker(self):
        resp = self.client.get("/api/score-radar?ticker=7203")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("trend", data)
        self.assertIn("pattern_a", data)
        self.assertIn("pattern_b", data)
        self.assertIn("liquidity", data)
        self.assertEqual(data["trend"], 80)
        self.assertEqual(data["pattern_a"], 65)

    def test_api_score_radar_nonexistent_ticker(self):
        resp = self.client.get("/api/score-radar?ticker=ZZZZ")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["trend"], 0)

    # ---- /api/price-history ----

    def test_api_price_history_missing_ticker_returns_400(self):
        resp = self.client.get("/api/price-history")
        self.assertEqual(resp.status_code, 400)

    def test_api_price_history_valid(self):
        resp = self.client.get("/api/price-history?ticker=7203&days=10")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("dates", data)
        self.assertIn("close", data)
        self.assertIn("ma25", data)
        self.assertIn("ma75", data)
        self.assertLessEqual(len(data["dates"]), 10)

    # ---- /api/market-timeline ----

    def test_api_market_timeline(self):
        resp = self.client.get("/api/market-timeline")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("dates", data)
        self.assertIn("n225", data)
        self.assertIn("sp500", data)
        self.assertIn("modes", data)

    # ---- /api/perf-monthly ----

    def test_api_perf_monthly(self):
        resp = self.client.get("/api/perf-monthly")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("months", data)
        self.assertIn("pnl", data)
        self.assertIn("trades", data)

    # ---- /api/perf-cumulative ----

    def test_api_perf_cumulative(self):
        resp = self.client.get("/api/perf-cumulative")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("dates", data)
        self.assertIn("cumulative_pnl", data)

    # ---- /api/sector-alloc ----

    def test_api_sector_alloc(self):
        resp = self.client.get("/api/sector-alloc")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("sectors", data)
        self.assertIn("values", data)


if __name__ == "__main__":
    unittest.main()
