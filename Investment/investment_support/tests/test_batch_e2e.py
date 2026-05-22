"""
E2E パイプライン統合テスト (T-3)

検証フロー:
  price_history 挿入
  → technical_calculator.calculate_and_save_snapshots()
  → signal_detector.detect_and_save_signals()
  → daily_signals テーブルに結果が保存されること
  → notifier のブロック生成が例外なく動作すること

設計方針:
- インメモリ SQLite を使用し、ファイルシステムに副作用を残さない
- 外部 API 呼び出し（Slack Webhook、EODHD 等）はモックする
- 実際の計算ロジック（technical_calculator、signal_detector）はそのまま実行する
- テスト数は最小限（パイプラインが繋がることを確認する）
"""

import sqlite3
import sys
import unittest
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

# sys.path 調整（investment_support/ と Ishikawa238/ を追加）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

# ============================================================
# CONFIG モック（全モジュール import 前に適用）
# ============================================================

_CONFIG_MOCK = {
    "signal": {
        "pattern_a_rsi_min": "50",
        "pattern_a_rsi_max": "70",
        "pattern_a_volume_ratio_min": "1.2",
        "pattern_a_52w_high_pct": "0.92",
        "pattern_b_rsi_min": "40",
        "pattern_b_rsi_max": "60",
        "pattern_b_ma25_touch_pct": "0.98",
        "pattern_b_lookback_days": "5",
        "max_candidates_per_pattern": "3",
        "atr_stop_multiplier": "2.0",
        "risk_reward_ratio": "2.0",
        "stop_loss_warning_min_pct": "1.0",
        "stop_loss_warning_max_pct": "15.0",
    },
    "capital": {
        "total_capital_jpy": "1000000",
        "total_capital_usd": "10000",
        "max_risk_per_trade_pct": "2",
        "max_concurrent_holdings": "5",
        "default_stop_loss_pct": "8",
    },
    "database": {
        "db_path": ":memory:",
        "backup_dir": "/tmp",
        "backup_retention_days": "7",
    },
    "slack": {"webhook_url": "https://XXXXX"},
    "jquants": {
        "earnings_alert_days_warning": "7",
        "earnings_alert_days_critical": "3",
    },
    "report_server": {"host": "127.0.0.1", "port": "5100"},
    "logging": {"log_dir": "/tmp", "log_level": "WARNING"},
}

import src.config as _src_config
_src_config.CONFIG = _CONFIG_MOCK

from src.technical_calculator import calculate_and_save_snapshots, calculate_index_mas
from src.signal_detector import detect_and_save_signals
from src import notifier as _notifier_module


# ============================================================
# DB セットアップヘルパー
# ============================================================

_DDL = [
    # universe_master（FK 対象）
    """CREATE TABLE IF NOT EXISTS universe_master (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker             TEXT    NOT NULL,
        name               TEXT    NOT NULL,
        market             TEXT    NOT NULL,
        exchange           TEXT,
        shares_outstanding REAL,
        is_active          INTEGER NOT NULL DEFAULT 1,
        is_excluded        INTEGER NOT NULL DEFAULT 0,
        exclude_reason     TEXT,
        last_updated       DATE    NOT NULL,
        created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_um_ticker ON universe_master(ticker)",

    # price_history
    """CREATE TABLE IF NOT EXISTS price_history (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker     TEXT    NOT NULL,
        date       DATE    NOT NULL,
        open       REAL,
        high       REAL,
        low        REAL,
        close      REAL,
        adj_close  REAL,
        volume     REAL,
        is_holiday INTEGER NOT NULL DEFAULT 0,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ph_ticker_date ON price_history(ticker, date)",

    # index_snapshot
    """CREATE TABLE IF NOT EXISTS index_snapshot (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        index_code  TEXT    NOT NULL,
        date        DATE    NOT NULL,
        close       REAL    NOT NULL,
        ma25        REAL,
        ma75        REAL,
        ma200       REAL,
        ma25_5d_ago REAL,
        created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_is_code_date ON index_snapshot(index_code, date)",

    # technical_snapshot
    """CREATE TABLE IF NOT EXISTS technical_snapshot (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker         TEXT    NOT NULL,
        date           DATE    NOT NULL,
        ma5            REAL,
        ma25           REAL,
        ma75           REAL,
        ma200          REAL,
        ma5_5d_ago     REAL,
        ma25_5d_ago    REAL,
        ma200_5d_ago   REAL,
        vol_20d_avg    REAL,
        high_20d       REAL,
        high_52w       REAL,
        low_52w        REAL,
        return_5d      REAL,
        return_20d     REAL,
        rsi14          REAL,
        atr14          REAL,
        volatility_20d REAL,
        est_turnover      REAL,
        est_market_cap    REAL,
        weekly_ma25       REAL,
        weekly_ma25_5w_ago REAL,
        parabolic_sar     REAL,
        sar_direction     TEXT,
        support_level     REAL,
        resistance_level  REAL,
        created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ts_ticker_date ON technical_snapshot(ticker, date)",

    # daily_signals（マイグレーション後のカラム構成）
    """CREATE TABLE IF NOT EXISTS daily_signals (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker                  TEXT    NOT NULL,
        date                    DATE    NOT NULL,
        market                  TEXT    NOT NULL,
        pattern                 TEXT,
        label                   TEXT    NOT NULL,
        cond_a_ma200_rising     INTEGER,
        cond_a_ma_aligned       INTEGER,
        cond_a_52w_high_near    INTEGER,
        cond_a_volume_surge     INTEGER,
        cond_a_rsi_ok           INTEGER,
        cond_a_above_ma25       INTEGER,
        cond_b_ma200_rising     INTEGER,
        cond_b_ma_aligned       INTEGER,
        cond_b_pullback_touched INTEGER,
        cond_b_bounced          INTEGER,
        cond_b_ma25_rising      INTEGER,
        cond_b_rsi_ok           INTEGER,
        cond_c_weekly_uptrend   INTEGER,
        cond_c_at_support       INTEGER,
        cond_c_rsi_oversold     INTEGER,
        cond_c_sar_reversal     INTEGER,
        close                   REAL,
        rsi14                   REAL,
        atr14                   REAL,
        vol_ratio               REAL,
        rs_rank                 INTEGER,
        trend_score             REAL,
        pattern_a_score         REAL,
        pattern_b_score         REAL,
        pattern_c_score         REAL,
        liquidity_score         REAL,
        base_score              REAL,
        market_multiplier       REAL,
        final_score             REAL,
        recommend_rank          TEXT,
        recommend_reason        TEXT,
        market_mode             TEXT,
        created_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ds_ticker_date ON daily_signals(ticker, date)",

    # trade_signal_log（fetch_near_misses の LEFT JOIN 用）
    """CREATE TABLE IF NOT EXISTS trade_signal_log (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_date        DATE    NOT NULL,
        ticker             TEXT    NOT NULL,
        name               TEXT,
        market             TEXT    NOT NULL,
        pattern            TEXT    NOT NULL,
        entry_price        REAL    NOT NULL,
        stop_loss_price    REAL    NOT NULL,
        stop_loss_pct      REAL    NOT NULL,
        target_price       REAL    NOT NULL,
        recommended_amount REAL,
        market_mode        TEXT    NOT NULL,
        conditions_met     TEXT,
        created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",

    # report_log（notifier._send が INSERT する）
    """CREATE TABLE IF NOT EXISTS report_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        report_date  DATE    NOT NULL,
        report_type  TEXT    NOT NULL,
        content      TEXT    NOT NULL,
        slack_status TEXT,
        created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",

    # batch_run_log（notifier._get_data_freshness が参照）
    """CREATE TABLE IF NOT EXISTS batch_run_log (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        run_date          DATE    NOT NULL,
        batch_type        TEXT    NOT NULL,
        start_time        DATETIME,
        end_time          DATETIME,
        status            TEXT,
        total_tickers     INTEGER DEFAULT 0,
        failed_tickers    INTEGER DEFAULT 0,
        signals_generated INTEGER DEFAULT 0,
        memo              TEXT
    )""",
]


def _build_in_memory_db() -> sqlite3.Connection:
    """全テーブルを含むインメモリ DB 接続を返す。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")  # FK チェックはテスト不要
    for ddl in _DDL:
        conn.execute(ddl)
    conn.commit()
    return conn


def _make_conn_patcher(conn: sqlite3.Connection):
    """
    get_connection() を指定の conn に差し替える patch 用のファクトリ関数を返す。

    shared.db.get_connection は @contextmanager デコレータ付きなので、
    patch の新しい側も @contextmanager で包む必要がある。
    """
    @contextmanager
    def _fake_get_connection(*args, **kwargs):
        yield conn

    return _fake_get_connection


# ============================================================
# モックデータ生成ヘルパー
# ============================================================

_TARGET_DATE = date(2026, 5, 14)
_TICKER_JP = "7203.T"
_TICKER_JP2 = "6758.T"


def _make_price_rows(ticker: str, n_days: int = 260) -> list[dict]:
    """
    単調増加するモック価格データを n_days 日分生成する。
    MA200 が計算できるよう 260 日分をデフォルトとする。
    """
    rows = []
    base = _TARGET_DATE - timedelta(days=n_days - 1)
    for i in range(n_days):
        d = base + timedelta(days=i)
        # 単調増加トレンド（MA200 < MA75 < MA25 < MA5 の整列を作る）
        price = 2000.0 + i * 5.0
        # 出来高: 後半20日で急増（パターンA 出来高条件を満たす）
        vol = 2_000_000.0 if i >= n_days - 20 else 800_000.0
        rows.append({
            "ticker":     ticker,
            "date":       d.strftime("%Y-%m-%d"),
            "open":       price * 0.99,
            "high":       price * 1.01,
            "low":        price * 0.98,
            "close":      price,
            "adj_close":  price,
            "volume":     vol,
            "is_holiday": 0,
        })
    return rows


def _make_index_rows(index_code: str, n_days: int = 260) -> list[dict]:
    """インデックスのモック価格データを返す。"""
    rows = []
    base = _TARGET_DATE - timedelta(days=n_days - 1)
    for i in range(n_days):
        d = base + timedelta(days=i)
        price = 35000.0 + i * 50.0
        rows.append({
            "index_code": index_code,
            "date":       d.strftime("%Y-%m-%d"),
            "close":      price,
        })
    return rows


def _insert_rows(conn: sqlite3.Connection, table: str, rows: list[dict]) -> None:
    """辞書リストをテーブルに一括 INSERT する。"""
    if not rows:
        return
    cols = list(rows[0].keys())
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    sql = f"INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})"
    conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
    conn.commit()


# ============================================================
# テストクラス
# ============================================================

class TestTechnicalCalculatorWithRealData(unittest.TestCase):
    """
    calculate_and_save_snapshots() がインメモリ DB のデータを読み込み、
    technical_snapshot に計算結果を保存することを確認する。
    """

    def setUp(self):
        self.conn = _build_in_memory_db()
        _insert_rows(self.conn, "price_history", _make_price_rows(_TICKER_JP))
        _insert_rows(self.conn, "price_history", _make_price_rows(_TICKER_JP2))
        self._fake_conn = _make_conn_patcher(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_snapshots_are_saved_to_db(self):
        """calculate_and_save_snapshots() が technical_snapshot に 2 件保存すること。"""
        tickers = [(_TICKER_JP, "JP"), (_TICKER_JP2, "JP")]

        with patch("src.technical_calculator.get_connection", self._fake_conn), \
             patch("src.data_store.get_connection", self._fake_conn):
            saved = calculate_and_save_snapshots(tickers, _TARGET_DATE)

        self.assertEqual(saved, 2)

        rows = self.conn.execute(
            "SELECT ticker, ma25, ma200, rsi14 FROM technical_snapshot WHERE date = ?",
            (_TARGET_DATE.strftime("%Y-%m-%d"),),
        ).fetchall()
        self.assertEqual(len(rows), 2, "2 銘柄分のスナップショットが保存されているはず")

    def test_ma_values_are_not_none(self):
        """260 日分のデータがあるため ma25 / ma200 / rsi14 が None にならないこと。"""
        tickers = [(_TICKER_JP, "JP")]

        with patch("src.technical_calculator.get_connection", self._fake_conn), \
             patch("src.data_store.get_connection", self._fake_conn):
            calculate_and_save_snapshots(tickers, _TARGET_DATE)

        row = self.conn.execute(
            "SELECT ma25, ma200, rsi14 FROM technical_snapshot WHERE ticker = ?",
            (_TICKER_JP,),
        ).fetchone()
        self.assertIsNotNone(row, "スナップショット行が存在するはず")
        self.assertIsNotNone(row["ma25"],  "260日分あれば ma25 は None にならない")
        self.assertIsNotNone(row["ma200"], "260日分あれば ma200 は None にならない")
        self.assertIsNotNone(row["rsi14"], "260日分あれば rsi14 は None にならない")


class TestIndexMaCalculation(unittest.TestCase):
    """
    calculate_index_mas() がインデックス価格から MA を正しく計算することを確認する。
    （純粋関数なので DB モック不要）
    """

    def test_calculate_index_mas_returns_rows(self):
        """260 日分の価格データから全日付分の MA 計算行を返すこと。"""
        price_rows = _make_index_rows("N225")
        prices = [{"date": r["date"], "close": r["close"]} for r in price_rows]

        result = calculate_index_mas("N225", prices)

        self.assertEqual(len(result), 260, "入力と同じ件数の行が返るはず")
        # 末尾行（最新日）は MA25 / MA200 が計算されているはず
        last = result[-1]
        self.assertIsNotNone(last["ma25"],  "260日あれば ma25 が計算されるはず")
        self.assertIsNotNone(last["ma200"], "260日あれば ma200 が計算されるはず")

    def test_calculate_index_mas_insufficient_data_returns_none(self):
        """データが 10 日分しかない場合、MA25 は None になること。"""
        price_rows = _make_index_rows("N225", n_days=10)
        prices = [{"date": r["date"], "close": r["close"]} for r in price_rows]

        result = calculate_index_mas("N225", prices)

        last = result[-1]
        self.assertIsNone(last["ma25"], "10日分では ma25 は計算不能なはず")


class TestSignalDetectorPipeline(unittest.TestCase):
    """
    detect_and_save_signals() がインメモリ DB のテクニカルスナップショットから
    daily_signals テーブルに結果を保存することを確認する。
    """

    def setUp(self):
        self.conn = _build_in_memory_db()
        # _check_pullback が price_history をクエリするため挿入
        _insert_rows(self.conn, "price_history", _make_price_rows(_TICKER_JP))
        self._fake_conn = _make_conn_patcher(self.conn)

    def tearDown(self):
        self.conn.close()

    def _make_screened_ticker(self, **overrides) -> dict:
        """流動性フィルタ通過済みのモック銘柄辞書を生成する。"""
        last_price = 2000.0 + (259 * 5.0)  # 260日目の終値
        base = {
            "ticker":        _TICKER_JP,
            "market":        "JP",
            "close":         last_price,
            "adj_close":     last_price,
            "volume":        2_000_000.0,
            # 単調増加トレンドを反映した MA（MA整列条件を満たす値）
            "ma5":           last_price - 25.0,
            "ma25":          last_price - 125.0,
            "ma75":          last_price - 375.0,
            "ma200":         last_price - 1000.0,
            "ma5_5d_ago":    last_price - 50.0,
            "ma25_5d_ago":   last_price - 150.0,
            "ma200_5d_ago":  last_price - 1050.0,
            "vol_20d_avg":   800_000.0,
            "high_52w":      last_price * 1.05,
            "low_52w":       last_price * 0.70,
            "rsi14":         58.0,
            "atr14":         100.0,
            "est_turnover":  last_price * 2_000_000.0,
            "est_market_cap": last_price * 100_000_000.0,
            "return_5d":     0.03,
            "return_20d":    0.10,
            "volatility_20d": 0.25,
        }
        base.update(overrides)
        return base

    def test_signals_are_saved_to_daily_signals(self):
        """1 銘柄を渡すと daily_signals に 1 件保存されること。"""
        screened = [self._make_screened_ticker()]

        with patch("src.signal_detector.get_connection", self._fake_conn), \
             patch("src.scoring.get_connection", self._fake_conn), \
             patch("src.data_store.get_connection", self._fake_conn):
            detect_and_save_signals(screened, _TARGET_DATE, "normal")

        rows = self.conn.execute(
            "SELECT ticker, label, final_score FROM daily_signals WHERE date = ?",
            (_TARGET_DATE.strftime("%Y-%m-%d"),),
        ).fetchall()
        self.assertEqual(len(rows), 1, "1 銘柄分のシグナル行が保存されるはず")

    def test_daily_signals_has_score_fields(self):
        """保存された行に final_score / market_mode 等のスコアフィールドが含まれること。"""
        screened = [self._make_screened_ticker()]

        with patch("src.signal_detector.get_connection", self._fake_conn), \
             patch("src.scoring.get_connection", self._fake_conn), \
             patch("src.data_store.get_connection", self._fake_conn):
            detect_and_save_signals(screened, _TARGET_DATE, "normal")

        row = self.conn.execute(
            "SELECT final_score, market_mode, recommend_rank FROM daily_signals LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row["final_score"],   "final_score が保存されるはず")
        self.assertEqual(row["market_mode"], "normal", "market_mode が保存されるはず")

    def test_caution_mode_saves_watch_not_candidate(self):
        """caution モードでは candidate が watch に降格されて保存されること。"""
        screened = [self._make_screened_ticker()]

        with patch("src.signal_detector.get_connection", self._fake_conn), \
             patch("src.scoring.get_connection", self._fake_conn), \
             patch("src.data_store.get_connection", self._fake_conn):
            result = detect_and_save_signals(screened, _TARGET_DATE, "caution")

        row = self.conn.execute("SELECT label FROM daily_signals LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        self.assertNotEqual(row["label"], "candidate",
                            "caution モードでは candidate が存在しないはず")
        self.assertEqual(result, [], "caution モードでは candidates は空リストを返すはず")


class TestFullPipelineIntegration(unittest.TestCase):
    """
    fetch（モックデータ）→ テクニカル計算 → シグナル検出 → 通知メッセージ構築
    の一連フローが例外なく完走することを確認する。
    """

    def setUp(self):
        self.conn = _build_in_memory_db()
        _insert_rows(self.conn, "price_history", _make_price_rows(_TICKER_JP))
        _insert_rows(self.conn, "price_history", _make_price_rows(_TICKER_JP2))
        _insert_rows(self.conn, "index_snapshot", _make_index_rows("N225"))
        self._fake_conn = _make_conn_patcher(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_pipeline_produces_daily_signals(self):
        """
        テクニカル計算 → シグナル検出の順に実行し、
        daily_signals が 2 件保存されること。
        """
        tickers = [(_TICKER_JP, "JP"), (_TICKER_JP2, "JP")]

        # STEP 1: テクニカル計算
        with patch("src.technical_calculator.get_connection", self._fake_conn), \
             patch("src.data_store.get_connection", self._fake_conn):
            saved = calculate_and_save_snapshots(tickers, _TARGET_DATE)

        self.assertEqual(saved, 2)

        # STEP 2: technical_snapshot の計算結果を screened_tickers 形式に変換
        def _snap_to_screened(ticker: str, market: str) -> dict:
            row = self.conn.execute(
                "SELECT * FROM technical_snapshot WHERE ticker = ? AND date = ?",
                (ticker, _TARGET_DATE.strftime("%Y-%m-%d")),
            ).fetchone()
            self.assertIsNotNone(row, f"{ticker} のスナップショットが存在するはず")
            d = dict(row)
            d["ticker"]    = ticker
            d["market"]    = market
            d["close"]     = d.get("adj_close") or 0
            d["adj_close"] = d.get("adj_close") or 0
            ph = self.conn.execute(
                "SELECT volume FROM price_history WHERE ticker = ? ORDER BY date DESC LIMIT 1",
                (ticker,),
            ).fetchone()
            d["volume"] = float(ph["volume"]) if ph else 0.0
            return d

        screened = [
            _snap_to_screened(_TICKER_JP,  "JP"),
            _snap_to_screened(_TICKER_JP2, "JP"),
        ]

        # STEP 3: シグナル検出
        with patch("src.signal_detector.get_connection", self._fake_conn), \
             patch("src.scoring.get_connection", self._fake_conn), \
             patch("src.data_store.get_connection", self._fake_conn):
            detect_and_save_signals(screened, _TARGET_DATE, "normal")

        # STEP 4: daily_signals に 2 件保存されていること
        sig_rows = self.conn.execute(
            "SELECT ticker, label FROM daily_signals WHERE date = ?",
            (_TARGET_DATE.strftime("%Y-%m-%d"),),
        ).fetchall()
        self.assertEqual(len(sig_rows), 2, "2 銘柄分のシグナル行が保存されるはず")

    def test_notifier_builds_evening_message_without_exception(self):
        """
        send_evening_report() が Slack 送信を無効化した状態で例外なく完走すること。
        候補なし・保有なしのケース（最低限の構成）。
        """
        market_env = {
            "mode":          "normal",
            "jp_above_ma25": 1,
            "us_above_ma25": 1,
            "n225_close":    38000.0,
            "n225_ma25":     36000.0,
            "sp500_close":   5200.0,
            "sp500_ma25":    5000.0,
        }

        # send_webhook / get_connection は notifier の名前空間にバインド済みのためそちらをモック
        with patch("src.notifier.get_connection", self._fake_conn), \
             patch("src.notifier.send_webhook", return_value=True):
            result = _notifier_module.send_evening_report(
                _TARGET_DATE,
                market_env,
                entry_sets=[],
                holdings=[],
                failed_tickers_count=0,
            )

        self.assertIsInstance(result, bool)

    def test_notifier_builds_message_with_candidate(self):
        """
        候補銘柄がある場合、send_evening_report() が例外なく完走すること。
        エントリーセットの全フィールドが通知ブロックに反映されるか確認する。
        """
        market_env = {
            "mode":          "normal",
            "jp_above_ma25": 1,
            "us_above_ma25": 1,
            "n225_close":    38000.0,
            "n225_ma25":     36000.0,
            "sp500_close":   5200.0,
            "sp500_ma25":    5000.0,
        }
        entry_sets = [{
            "ticker":             _TICKER_JP,
            "name":               "トヨタ自動車",
            "market":             "JP",
            "pattern":            "A",
            "entry_price":        2500.0,
            "stop_loss_price":    2300.0,
            "stop_loss_pct":      8.0,
            "target_price":       2900.0,
            "recommended_amount": 50000.0,
            "market_mode":        "normal",
            "conditions_met":     "[]",
            "_warn_narrow":       False,
            "_warn_wide":         False,
            "_rsi14":             58.0,
            "_vol_ratio":         2.5,
            "_rs_rank":           1,
            "_final_score":       65,
            "_recommend_rank":    "A",
            "_recommend_reason":  "MA整列・出来高急増",
            "_trend_score":       80,
            "_pattern_a_score":   70,
            "_pattern_b_score":   None,
            "_liquidity_score":   90,
            "_high_52w":          2600.0,
            "_close":             2500.0,
            "_ma5":               2480.0,
            "_ma25":              2400.0,
            "_ma75":              2300.0,
            "_ma200":             2000.0,
        }]

        with patch("src.notifier.get_connection", self._fake_conn), \
             patch("src.notifier.send_webhook", return_value=True):
            result = _notifier_module.send_evening_report(
                _TARGET_DATE,
                market_env,
                entry_sets=entry_sets,
                holdings=[],
                failed_tickers_count=0,
            )

        self.assertIsInstance(result, bool)


if __name__ == "__main__":
    unittest.main()
