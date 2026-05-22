"""
DB初期化スクリプト

実行方法（investment_support/ ディレクトリから）:
    python -m scripts.init_db

処理内容:
1. data/ ディレクトリを作成する
2. SQLite DB ファイルを新規作成する
3. 全テーブルの DDL を実行する
4. 初期化結果をコンソールに表示する

注意:
- 既存の DB ファイルがある場合は上書きせず、テーブルが存在しない場合のみ追加する
  （IF NOT EXISTS を使用しているため冪等に実行できる）
- DB を完全にリセットしたい場合は data/investment.db を手動で削除してから実行する
"""

import sqlite3
import sys
from pathlib import Path

# プロジェクトルートを sys.path に追加（モジュールの import のため）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent  # Ishikawa238/ — shared パッケージの検索に必要
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

from src.config import CONFIG
from src.logger import get_logger

logger = get_logger(__name__)

# ============================================================
# DDL 定義
# ============================================================

DDL_STATEMENTS = [
    # ----------------------------------------------------------
    # 銘柄マスタ
    # ----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS universe_master (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker              TEXT    NOT NULL,
        name                TEXT    NOT NULL,
        market              TEXT    NOT NULL CHECK(market IN ('JP', 'US')),
        exchange            TEXT,
        shares_outstanding  REAL,
        is_active           INTEGER NOT NULL DEFAULT 1,
        is_excluded         INTEGER NOT NULL DEFAULT 0,
        exclude_reason      TEXT,
        last_updated        DATE    NOT NULL,
        created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_universe_master_ticker ON universe_master(ticker)",
    "CREATE INDEX IF NOT EXISTS idx_universe_master_market ON universe_master(market, is_active, is_excluded)",

    # ----------------------------------------------------------
    # 日足価格履歴
    # ----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS price_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker      TEXT    NOT NULL,
        date        DATE    NOT NULL,
        open        REAL,
        high        REAL,
        low         REAL,
        close       REAL,
        adj_close   REAL,
        volume      REAL,
        is_holiday  INTEGER NOT NULL DEFAULT 0,
        created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (ticker) REFERENCES universe_master(ticker)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_price_history_ticker_date ON price_history(ticker, date)",
    "CREATE INDEX IF NOT EXISTS idx_price_history_date ON price_history(date)",

    # ----------------------------------------------------------
    # 日次テクニカル指標
    # ----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS technical_snapshot (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker          TEXT    NOT NULL,
        date            DATE    NOT NULL,
        ma5             REAL,
        ma25            REAL,
        ma75            REAL,
        ma200           REAL,
        ma5_5d_ago      REAL,
        ma25_5d_ago     REAL,
        ma200_5d_ago    REAL,
        vol_20d_avg     REAL,
        high_20d        REAL,
        high_52w        REAL,
        low_52w         REAL,
        return_5d       REAL,
        return_20d      REAL,
        rsi14           REAL,
        atr14           REAL,
        volatility_20d  REAL,
        est_turnover    REAL,
        est_market_cap  REAL,
        created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (ticker) REFERENCES universe_master(ticker)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_technical_snapshot_ticker_date ON technical_snapshot(ticker, date)",
    "CREATE INDEX IF NOT EXISTS idx_technical_snapshot_date ON technical_snapshot(date)",

    # ----------------------------------------------------------
    # 市場インデックス日次データ
    # ----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS index_snapshot (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        index_code  TEXT    NOT NULL,
        date        DATE    NOT NULL,
        close       REAL    NOT NULL,
        ma25        REAL,
        ma75        REAL,
        ma200       REAL,
        ma25_5d_ago REAL,
        created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_index_snapshot_code_date ON index_snapshot(index_code, date)",

    # ----------------------------------------------------------
    # 市場環境モード
    # ----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS market_environment (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        date            DATE    NOT NULL UNIQUE,
        jp_above_ma25   INTEGER,
        us_above_ma25   INTEGER,
        mode            TEXT    NOT NULL CHECK(mode IN ('normal', 'caution', 'full_caution')),
        n225_close      REAL,
        n225_ma25       REAL,
        sp500_close     REAL,
        sp500_ma25      REAL,
        created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,

    # ----------------------------------------------------------
    # 保有銘柄
    # ----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS holdings (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker           TEXT    NOT NULL,
        name             TEXT,
        market           TEXT    NOT NULL CHECK(market IN ('JP', 'US')),
        entry_price      REAL    NOT NULL,
        entry_date       DATE    NOT NULL,
        quantity         REAL,
        stop_loss_price  REAL,
        memo             TEXT,
        is_closed        INTEGER NOT NULL DEFAULT 0,
        close_price      REAL,
        close_date       DATE,
        close_reason     TEXT CHECK(close_reason IN ('stop_loss', 'take_profit', 'manual', NULL)),
        created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_holdings_active ON holdings(is_closed, ticker)",

    # ----------------------------------------------------------
    # 日次シグナル（判定結果）
    # ----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS daily_signals (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker                  TEXT    NOT NULL,
        date                    DATE    NOT NULL,
        market                  TEXT    NOT NULL CHECK(market IN ('JP', 'US')),
        pattern                 TEXT    CHECK(pattern IN ('A', 'B', 'C', NULL)),
        label                   TEXT    NOT NULL CHECK(label IN ('candidate', 'watch', 'skip')),
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
        close                   REAL,
        rsi14                   REAL,
        atr14                   REAL,
        vol_ratio               REAL,
        rs_rank                 INTEGER,
        created_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (ticker) REFERENCES universe_master(ticker)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_signals_ticker_date ON daily_signals(ticker, date)",
    "CREATE INDEX IF NOT EXISTS idx_daily_signals_date_label ON daily_signals(date, label)",

    # ----------------------------------------------------------
    # シグナル発信ログ（エントリーセット）
    # ----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS trade_signal_log (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_date         DATE    NOT NULL,
        ticker              TEXT    NOT NULL,
        name                TEXT,
        market              TEXT    NOT NULL CHECK(market IN ('JP', 'US')),
        pattern             TEXT    NOT NULL CHECK(pattern IN ('A', 'B', 'C')),
        entry_price         REAL    NOT NULL,
        stop_loss_price     REAL    NOT NULL,
        stop_loss_pct       REAL    NOT NULL,
        target_price        REAL    NOT NULL,
        recommended_amount  REAL,
        market_mode         TEXT    NOT NULL,
        conditions_met      TEXT,
        created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_trade_signal_log_date ON trade_signal_log(signal_date)",
    "CREATE INDEX IF NOT EXISTS idx_trade_signal_log_ticker ON trade_signal_log(ticker)",

    # ----------------------------------------------------------
    # トレード実績ログ（手動入力）
    # ----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS trade_result_log (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id           INTEGER NOT NULL,
        ticker              TEXT    NOT NULL,
        actual_entry_price  REAL,
        actual_entry_date   DATE,
        actual_exit_price   REAL,
        actual_exit_date    DATE,
        exit_reason         TEXT CHECK(exit_reason IN ('stop_loss', 'take_profit', 'manual', 'forced_stop', NULL)),
        pnl_pct             REAL,
        pnl_amount          REAL,
        memo                TEXT,
        created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (signal_id) REFERENCES trade_signal_log(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_trade_result_log_signal ON trade_result_log(signal_id)",

    # ----------------------------------------------------------
    # 通知本文ログ
    # ----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS report_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        report_date  DATE    NOT NULL,
        report_type  TEXT    NOT NULL CHECK(report_type IN ('morning', 'evening', 'monthly', 'morning_skip', 'evening_skip')),
        content      TEXT    NOT NULL,
        slack_status TEXT    CHECK(slack_status IN ('success', 'failed', 'skipped')),
        created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_report_log_date ON report_log(report_date, report_type)",

    # ----------------------------------------------------------
    # エラーログ
    # ----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS error_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        error_time    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        batch_type    TEXT,
        module        TEXT,
        ticker        TEXT,
        error_type    TEXT,
        error_message TEXT,
        is_fatal      INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_error_log_time ON error_log(error_time)",

    # ----------------------------------------------------------
    # バッチ実行ログ
    # ----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS batch_run_log (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        run_date          DATE    NOT NULL,
        batch_type        TEXT    NOT NULL CHECK(batch_type IN ('evening_jp', 'morning_all', 'monthly')),
        start_time        DATETIME,
        end_time          DATETIME,
        status            TEXT    CHECK(status IN ('running', 'success', 'partial_failure', 'failed')),
        total_tickers     INTEGER DEFAULT 0,
        failed_tickers    INTEGER DEFAULT 0,
        signals_generated INTEGER DEFAULT 0,
        memo              TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_batch_run_log_date ON batch_run_log(run_date, batch_type)",

    # ----------------------------------------------------------
    # 決算発表予定日（J-Quants fins/announcement から取得）
    # ----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS earnings_schedule (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker            TEXT    NOT NULL,
        announcement_date DATE    NOT NULL,
        fiscal_year       TEXT,
        fiscal_quarter    TEXT,
        fetched_at        DATE    NOT NULL,
        created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (ticker) REFERENCES universe_master(ticker)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_earnings_schedule_ticker_date ON earnings_schedule(ticker, announcement_date)",
    "CREATE INDEX IF NOT EXISTS idx_earnings_schedule_date ON earnings_schedule(announcement_date)",

    # ----------------------------------------------------------
    # 財務スナップショット（J-Quants fins/statements から取得）
    # ----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS fundamentals_snapshot (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker              TEXT    NOT NULL,
        fiscal_year         TEXT    NOT NULL,
        fiscal_quarter      TEXT    NOT NULL,
        revenue             REAL,
        operating_income    REAL,
        ordinary_income     REAL,
        net_income          REAL,
        eps                 REAL,
        bps                 REAL,
        shares_outstanding  REAL,
        dividend_per_share  REAL,
        per                 REAL,
        pbr                 REAL,
        dividend_yield      REAL,
        fetched_at          DATE    NOT NULL,
        created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (ticker) REFERENCES universe_master(ticker)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_fundamentals_ticker_period ON fundamentals_snapshot(ticker, fiscal_year, fiscal_quarter)",

    # ----------------------------------------------------------
    # リスク指標キャッシュ (Phase 3)
    # ----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS risk_snapshot (
        ticker       TEXT    NOT NULL,
        calc_date    TEXT    NOT NULL,
        var_99_1d    REAL,
        beta_60d     REAL,
        sharpe_60d   REAL,
        updated_at   TEXT    NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (ticker, calc_date)
    )
    """,

    # ----------------------------------------------------------
    # ペーパートレード（仮想売買記録）
    # ----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS paper_trades (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_date      DATE    NOT NULL,
        ticker           TEXT    NOT NULL,
        name             TEXT,
        market           TEXT    NOT NULL CHECK(market IN ('JP', 'US')),
        pattern          TEXT    CHECK(pattern IN ('A', 'B', 'C', NULL)),
        entry_price      REAL    NOT NULL,
        planned_entry_price REAL,
        actual_entry_date DATE,
        actual_entry_price REAL,
        stop_loss_price  REAL    NOT NULL,
        target_price     REAL    NOT NULL,
        position_size    REAL,
        exit_price       REAL,
        exit_date        DATE,
        exit_reason      TEXT    CHECK(exit_reason IN ('stop_loss', 'take_profit', 'manual', NULL)),
        pnl_pct          REAL,
        pnl_amount       REAL,
        is_filled        INTEGER NOT NULL DEFAULT 0,
        entry_slippage_pct REAL NOT NULL DEFAULT 0,
        status           TEXT    NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'closed')),
        created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_paper_trades_status ON paper_trades(status)",
    "CREATE INDEX IF NOT EXISTS idx_paper_trades_ticker ON paper_trades(ticker, status)",
    "CREATE INDEX IF NOT EXISTS idx_paper_trades_fill ON paper_trades(status, is_filled)",

    # ----------------------------------------------------------
    # LLM フィードバックログ
    # ----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS llm_feedback_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        feedback_date DATE    NOT NULL,
        batch_type    TEXT    NOT NULL,
        provider      TEXT    NOT NULL,
        model         TEXT    NOT NULL,
        prompt_tokens INTEGER,
        output_tokens INTEGER,
        response_text TEXT    NOT NULL,
        latency_ms    INTEGER,
        created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_llm_feedback_date ON llm_feedback_log(feedback_date)",

    # ----------------------------------------------------------
    # 利確ターゲット管理
    # ----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS take_profit_targets (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker          TEXT    NOT NULL,
        profit_type     TEXT    NOT NULL CHECK(profit_type IN ('I', 'II', 'III')),
        r_value         REAL,
        t1_price        REAL,
        t1_pct          REAL,
        t1_reached      INTEGER NOT NULL DEFAULT 0,
        t1_reached_date DATE,
        t2_price        REAL,
        t2_pct          REAL,
        t2_reached      INTEGER NOT NULL DEFAULT 0,
        t2_reached_date DATE,
        t3_price        REAL,
        t3_pct          REAL,
        t3_reached      INTEGER NOT NULL DEFAULT 0,
        t3_reached_date DATE,
        adjusted_stop   REAL,
        trailing_active INTEGER NOT NULL DEFAULT 0,
        is_active       INTEGER NOT NULL DEFAULT 1,
        created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tp_targets_active ON take_profit_targets(ticker, is_active) WHERE is_active = 1",
    # ポートフォリオスナップショット（バッチ実行時の総資産記録）
    """
    CREATE TABLE IF NOT EXISTS portfolio_snapshot (
        id                        INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_date             DATE    NOT NULL,
        batch_type                TEXT    NOT NULL,
        capital_jpy               REAL    NOT NULL,
        capital_usd               REAL    NOT NULL,
        holdings_value_jpy        REAL    NOT NULL DEFAULT 0,
        holdings_value_usd        REAL    NOT NULL DEFAULT 0,
        unrealized_pnl_jpy        REAL    NOT NULL DEFAULT 0,
        unrealized_pnl_usd        REAL    NOT NULL DEFAULT 0,
        total_assets_jpy          REAL    NOT NULL DEFAULT 0,
        total_assets_usd          REAL    NOT NULL DEFAULT 0,
        total_assets_combined_jpy REAL    NOT NULL DEFAULT 0,
        cash_jpy                  REAL    NOT NULL DEFAULT 0,
        cash_usd                  REAL    NOT NULL DEFAULT 0,
        paypay_points_value_jpy   REAL    NOT NULL DEFAULT 0,
        usdjpy_rate               REAL    NOT NULL,
        holdings_count            INTEGER NOT NULL DEFAULT 0,
        created_at                DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ps_date_batch ON portfolio_snapshot(snapshot_date, batch_type)",

    # ----------------------------------------------------------
    # PayPay ポイント運用
    # ----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS paypay_points (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        course          TEXT    NOT NULL UNIQUE,
        course_name     TEXT    NOT NULL,
        invested_points INTEGER NOT NULL,
        current_points  INTEGER NOT NULL,
        last_updated    DATE    NOT NULL,
        created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
]

# 既存DBへのカラム追加（ALTER TABLE は IF NOT EXISTS 非対応のため try/except で処理）
MIGRATION_STATEMENTS = [
    "ALTER TABLE fundamentals_snapshot ADD COLUMN per            REAL",
    "ALTER TABLE fundamentals_snapshot ADD COLUMN pbr            REAL",
    "ALTER TABLE fundamentals_snapshot ADD COLUMN dividend_yield REAL",
    # v1 スコアリング: daily_signals にスコア列を追加
    "ALTER TABLE daily_signals ADD COLUMN trend_score       REAL",
    "ALTER TABLE daily_signals ADD COLUMN pattern_a_score   REAL",
    "ALTER TABLE daily_signals ADD COLUMN pattern_b_score   REAL",
    "ALTER TABLE daily_signals ADD COLUMN liquidity_score   REAL",
    "ALTER TABLE daily_signals ADD COLUMN base_score        REAL",
    "ALTER TABLE daily_signals ADD COLUMN market_multiplier REAL",
    "ALTER TABLE daily_signals ADD COLUMN final_score       REAL",
    "ALTER TABLE daily_signals ADD COLUMN recommend_rank    TEXT",
    "ALTER TABLE daily_signals ADD COLUMN recommend_reason  TEXT",
    "ALTER TABLE daily_signals ADD COLUMN market_mode       TEXT",
    # Phase 3: セクター・業種カラム追加
    "ALTER TABLE universe_master ADD COLUMN sector   TEXT",
    "ALTER TABLE universe_master ADD COLUMN industry TEXT",
    # portfolio_snapshot: 口座現金残高カラム追加
    "ALTER TABLE portfolio_snapshot ADD COLUMN cash_jpy REAL NOT NULL DEFAULT 0",
    "ALTER TABLE portfolio_snapshot ADD COLUMN cash_usd REAL NOT NULL DEFAULT 0",
    # portfolio_snapshot: PayPay ポイント運用残高カラム追加
    "ALTER TABLE portfolio_snapshot ADD COLUMN paypay_points_value_jpy REAL NOT NULL DEFAULT 0",
    # paper_trades: シグナル翌営業日の始値で約定させるための実約定カラム
    "ALTER TABLE paper_trades ADD COLUMN planned_entry_price REAL",
    "ALTER TABLE paper_trades ADD COLUMN actual_entry_date DATE",
    "ALTER TABLE paper_trades ADD COLUMN actual_entry_price REAL",
    "ALTER TABLE paper_trades ADD COLUMN is_filled INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE paper_trades ADD COLUMN entry_slippage_pct REAL NOT NULL DEFAULT 0",
    # Pattern C: technical_snapshot に週足MA・パラボリックSAR・サポート/レジスタンス追加
    "ALTER TABLE technical_snapshot ADD COLUMN weekly_ma25        REAL",
    "ALTER TABLE technical_snapshot ADD COLUMN weekly_ma25_5w_ago REAL",
    "ALTER TABLE technical_snapshot ADD COLUMN parabolic_sar      REAL",
    "ALTER TABLE technical_snapshot ADD COLUMN sar_direction       TEXT",
    "ALTER TABLE technical_snapshot ADD COLUMN support_level       REAL",
    "ALTER TABLE technical_snapshot ADD COLUMN resistance_level    REAL",
    # Pattern C: daily_signals に条件フラグ・スコア追加
    "ALTER TABLE daily_signals ADD COLUMN cond_c_weekly_uptrend INTEGER",
    "ALTER TABLE daily_signals ADD COLUMN cond_c_at_support      INTEGER",
    "ALTER TABLE daily_signals ADD COLUMN cond_c_rsi_oversold    INTEGER",
    "ALTER TABLE daily_signals ADD COLUMN cond_c_sar_reversal    INTEGER",
    "ALTER TABLE daily_signals ADD COLUMN pattern_c_score         REAL",
]

# 冪等なデータ修正マイグレーション（毎回実行しても安全）
DATA_MIGRATION_STATEMENTS = [
    # holdings テーブルの重複行クリーンアップ（同 ticker/is_closed=0 で id 最大以外を削除）
    # sync_holdings_from_csv() が INSERT OR REPLACE（UNIQUE制約なし）で重複を生じさせていた問題の修正
    """
    DELETE FROM holdings
    WHERE is_closed = 0
      AND id NOT IN (
          SELECT MAX(id) FROM holdings
          WHERE is_closed = 0
          GROUP BY ticker
      )
    """,
]


# ============================================================
# 初期化処理
# ============================================================

def init_db() -> None:
    """DB を初期化する。テーブルが既に存在する場合はスキップされる（冪等）。"""
    db_path = Path(CONFIG["database"]["db_path"])
    db_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"DB初期化開始: {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")

        success_count = 0
        skip_count = 0
        deferred_indexes: list[str] = []

        for i, ddl in enumerate(DDL_STATEMENTS, start=1):
            stmt = ddl.strip()
            if not stmt:
                continue
            try:
                conn.execute(stmt)
                # CREATE TABLE / INDEX の場合のみカウント表示
                if stmt.upper().startswith("CREATE TABLE"):
                    table_name = _extract_table_name(stmt)
                    logger.info(f"  テーブル作成（または確認）: {table_name}")
                    success_count += 1
                elif stmt.upper().startswith("CREATE"):
                    pass  # インデックスは個別表示しない
            except sqlite3.Error as e:
                # CREATE INDEX はマイグレーション追加カラムに依存する場合があるため、
                # エラー時はマイグレーション後にリトライする
                if stmt.upper().lstrip().startswith("CREATE INDEX") or \
                   stmt.upper().lstrip().startswith("CREATE UNIQUE INDEX"):
                    deferred_indexes.append(stmt)
                    logger.debug(f"  インデックス作成をマイグレーション後に延期: {stmt[:60]}...")
                else:
                    logger.error(f"  DDL実行エラー（ステップ{i}）: {e}\n  SQL: {stmt[:80]}...")
                    conn.close()
                    raise

        _run_migrations(conn)

        # マイグレーション後に延期されたインデックスをリトライ
        for stmt in deferred_indexes:
            try:
                conn.execute(stmt)
                logger.info(f"  延期インデックス作成成功: {stmt.strip()[:60]}")
            except sqlite3.OperationalError as e:
                if "already exists" in str(e).lower():
                    pass
                else:
                    logger.warning(f"  延期インデックス作成失敗: {e}")

        conn.commit()
        logger.info(f"DB初期化完了: テーブル {success_count} 件を作成（または確認）しました。")

    finally:
        conn.close()

    _print_table_summary(db_path)


def _migrate_report_log_check_for_skip(conn: sqlite3.Connection) -> None:
    """
    report_log の CHECK(report_type IN ('morning','evening','monthly')) を
    ('morning','evening','monthly','morning_skip','evening_skip') に拡張する。

    冪等: 'morning_skip' を INSERT できれば既に拡張済みとみなしてスキップする。
    """
    table = "report_log"
    # 拡張済みかテスト
    try:
        conn.execute(f"SAVEPOINT chk_{table}")
        conn.execute(
            "INSERT INTO report_log (report_date, report_type, content, slack_status) "
            "VALUES ('9999-12-31', 'morning_skip', '{}', 'skipped')"
        )
        conn.execute(
            "DELETE FROM report_log WHERE report_date = '9999-12-31' AND report_type = 'morning_skip'"
        )
        conn.execute(f"RELEASE chk_{table}")
        logger.info(f"  CHECK制約拡張済み（スキップ）: {table}.report_type")
        return
    except sqlite3.IntegrityError:
        conn.execute(f"ROLLBACK TO chk_{table}")
        conn.execute(f"RELEASE chk_{table}")
    except sqlite3.OperationalError:
        conn.execute(f"ROLLBACK TO chk_{table}")
        conn.execute(f"RELEASE chk_{table}")

    # テーブル再作成による制約拡張
    logger.info(f"  CHECK制約を拡張中: {table}.report_type")
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if not row:
            logger.warning(f"  テーブルが見つかりません: {table}")
            return

        original_ddl = row[0]
        new_ddl = original_ddl.replace(
            "CHECK(report_type IN ('morning', 'evening', 'monthly'))",
            "CHECK(report_type IN ('morning', 'evening', 'monthly', 'morning_skip', 'evening_skip'))",
        )
        if new_ddl == original_ddl:
            logger.info(f"  CHECK制約変更不要: {table}")
            return

        temp_ddl = new_ddl.replace(
            f"CREATE TABLE {table}",
            f"CREATE TABLE {table}__new",
            1,
        )

        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        conn.execute(f"SAVEPOINT migrate_{table}")
        conn.execute(temp_ddl)

        cols_info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        col_names = ", ".join(c[1] for c in cols_info)
        conn.execute(
            f"INSERT INTO {table}__new ({col_names}) "
            f"SELECT {col_names} FROM {table}"
        )

        old_count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        new_count = conn.execute(f"SELECT COUNT(*) FROM {table}__new").fetchone()[0]
        if old_count != new_count:
            conn.execute(f"ROLLBACK TO migrate_{table}")
            conn.execute(f"RELEASE migrate_{table}")
            conn.execute("PRAGMA foreign_keys = ON")
            logger.error(f"  行数不一致: {table} ({old_count} vs {new_count})")
            return

        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {table}__new RENAME TO {table}")
        conn.execute(f"RELEASE migrate_{table}")
        conn.execute("PRAGMA foreign_keys = ON")
        logger.info(f"  CHECK制約拡張完了: {table} ({old_count}行)")
    except Exception as e:
        logger.error(f"  CHECK制約拡張エラー: {table}: {e}")
        try:
            conn.execute(f"ROLLBACK TO migrate_{table}")
            conn.execute(f"RELEASE migrate_{table}")
        except Exception:
            pass
        try:
            conn.execute(f"DROP TABLE IF EXISTS {table}__new")
        except Exception:
            pass
        try:
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception:
            pass


def _migrate_check_constraints_for_pattern_c(conn: sqlite3.Connection) -> None:
    """
    既存DBの CHECK(pattern IN ('A','B')) 制約を ('A','B','C') に拡張する。

    SQLite は ALTER CONSTRAINT をサポートしないため、テーブルを再作成する。
    冪等: 'C' を INSERT できれば既に拡張済みとみなしてスキップする。
    """
    tables_to_migrate = [
        ("daily_signals", "pattern", True),       # NULL 許可
        ("trade_signal_log", "pattern", False),    # NOT NULL
        ("paper_trades", "pattern", True),         # NULL 許可
    ]

    for table, col, nullable in tables_to_migrate:
        # 拡張済みかテスト（SAVEPOINT でロールバック）
        try:
            conn.execute(f"SAVEPOINT chk_{table}")
            if nullable:
                conn.execute(
                    f"INSERT INTO {table} ({col}) VALUES ('C')"
                )
            else:
                # NOT NULL テーブルは全必須カラムを埋める必要がある
                # テスト行を作って即削除する
                conn.execute(f"SAVEPOINT chk_{table}")
                # trade_signal_log の必須カラムで試す
                conn.execute(
                    "INSERT INTO trade_signal_log "
                    "(signal_date, ticker, market, pattern, entry_price, "
                    "stop_loss_price, stop_loss_pct, target_price, market_mode) "
                    "VALUES ('9999-12-31','__CHK__','JP','C',0,0,0,0,'normal')"
                )
                conn.execute(
                    "DELETE FROM trade_signal_log WHERE ticker = '__CHK__'"
                )
            conn.execute(f"RELEASE chk_{table}")
            logger.info(f"  CHECK制約拡張済み（スキップ）: {table}.{col}")
            continue
        except sqlite3.IntegrityError:
            conn.execute(f"ROLLBACK TO chk_{table}")
            conn.execute(f"RELEASE chk_{table}")
        except sqlite3.OperationalError:
            conn.execute(f"ROLLBACK TO chk_{table}")
            conn.execute(f"RELEASE chk_{table}")

        # テーブル再作成による制約拡張
        logger.info(f"  CHECK制約を拡張中: {table}.{col}")
        try:
            # 現在のCREATE TABLE文を取得
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not row:
                logger.warning(f"  テーブルが見つかりません: {table}")
                continue

            original_ddl = row[0]
            # CHECK制約を書き換え
            new_ddl = original_ddl.replace(
                "CHECK(pattern IN ('A', 'B'))",
                "CHECK(pattern IN ('A', 'B', 'C'))",
            ).replace(
                "CHECK(pattern IN ('A', 'B', NULL))",
                "CHECK(pattern IN ('A', 'B', 'C', NULL))",
            )

            if new_ddl == original_ddl:
                logger.info(f"  CHECK制約変更不要: {table}")
                continue

            # テーブル名を一時名に変更した DDL
            temp_ddl = new_ddl.replace(
                f"CREATE TABLE {table}",
                f"CREATE TABLE {table}__new",
                1,
            )

            # 外部キー制約を一時的に無効化（DROP TABLE 時の FK 違反防止）
            # PRAGMA foreign_keys は保留中のトランザクションがない状態でのみ有効
            conn.commit()
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN")
            conn.execute(f"SAVEPOINT migrate_{table}")
            conn.execute(temp_ddl)

            # カラム一覧を取得してコピー
            cols_info = conn.execute(f"PRAGMA table_info({table})").fetchall()
            col_names = ", ".join(c[1] for c in cols_info)
            conn.execute(
                f"INSERT INTO {table}__new ({col_names}) "
                f"SELECT {col_names} FROM {table}"
            )

            # 行数検証
            old_count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            new_count = conn.execute(f"SELECT COUNT(*) FROM {table}__new").fetchone()[0]
            if old_count != new_count:
                conn.execute(f"ROLLBACK TO migrate_{table}")
                conn.execute(f"RELEASE migrate_{table}")
                conn.execute("PRAGMA foreign_keys = ON")
                logger.error(f"  行数不一致: {table} ({old_count} vs {new_count})")
                continue

            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {table}__new RENAME TO {table}")
            conn.execute(f"RELEASE migrate_{table}")
            conn.execute("PRAGMA foreign_keys = ON")
            logger.info(f"  CHECK制約拡張完了: {table} ({old_count}行)")
        except Exception as e:
            logger.error(f"  CHECK制約拡張エラー: {table}: {e}")
            try:
                conn.execute(f"ROLLBACK TO migrate_{table}")
                conn.execute(f"RELEASE migrate_{table}")
            except Exception:
                pass
            try:
                conn.execute(f"DROP TABLE IF EXISTS {table}__new")
            except Exception:
                pass
            try:
                conn.execute("PRAGMA foreign_keys = ON")
            except Exception:
                pass


def _run_migrations(conn: sqlite3.Connection) -> None:
    """
    既存DBに対してカラム追加などのマイグレーションを実行する。
    カラムが既に存在する場合は OperationalError を無視する（冪等）。
    """
    # Pattern C: CHECK制約拡張（カラム追加より先に実行）
    _migrate_check_constraints_for_pattern_c(conn)

    # report_log: skip通知用の report_type 拡張
    _migrate_report_log_check_for_skip(conn)

    for stmt in MIGRATION_STATEMENTS:
        try:
            conn.execute(stmt)
            logger.info(f"  マイグレーション実行: {stmt.strip()[:60]}")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                pass  # 既に存在する場合はスキップ
            else:
                raise

    # データ修正マイグレーション（冪等・エラー無視しない）
    for stmt in DATA_MIGRATION_STATEMENTS:
        try:
            cursor = conn.execute(stmt)
            affected = cursor.rowcount
            if affected > 0:
                logger.info(f"  データマイグレーション実行（{affected}行影響）: {stmt.strip()[:60]}")
        except sqlite3.OperationalError as e:
            logger.warning(f"  データマイグレーションスキップ: {e}")


def _extract_table_name(ddl: str) -> str:
    """DDL 文からテーブル名を抽出する。
    CREATE TABLE IF NOT EXISTS table_name → table_name を返す。
    """
    tokens = ddl.split()
    for i, token in enumerate(tokens):
        # "EXISTS" の次のトークンがテーブル名（CREATE TABLE IF NOT EXISTS <name>）
        if token.upper() == "EXISTS" and i + 1 < len(tokens):
            return tokens[i + 1].strip("(")
    # IF NOT EXISTS がない場合（CREATE TABLE <name>）
    for i, token in enumerate(tokens):
        if token.upper() == "TABLE" and i + 1 < len(tokens):
            return tokens[i + 1].strip("(")
    return "unknown"


def _print_table_summary(db_path: Path) -> None:
    """作成されたテーブルの一覧と行数をコンソールに表示する。"""
    import io
    # Windows の cp932 端末でも文字化けしないよう utf-8 ストリームに書き出す
    out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    def p(text: str = "") -> None:
        out.write(text + "\n")
        out.flush()

    conn = sqlite3.connect(db_path)
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()

        p("\n" + "=" * 50)
        p("  DB初期化完了サマリー")
        p("=" * 50)
        p(f"  DBファイル: {db_path.resolve()}")
        p(f"  テーブル数: {len(tables)}")
        p("-" * 50)
        for (table_name,) in tables:
            count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
            p(f"  {table_name:<35} {count:>6} 行")
        p("=" * 50)
        p("  次のステップ: python -m scripts.fetch_historical を実行して")
        p("  過去3年分のデータを初回取得してください。")
        p("=" * 50 + "\n")
    finally:
        conn.close()


# ============================================================
# エントリーポイント
# ============================================================

if __name__ == "__main__":
    try:
        init_db()
    except FileNotFoundError as e:
        print(f"\n[エラー] {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"DB初期化中に予期しないエラーが発生しました: {e}", exc_info=True)
        sys.exit(1)
