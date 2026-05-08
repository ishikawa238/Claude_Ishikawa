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
        pattern                 TEXT    CHECK(pattern IN ('A', 'B', NULL)),
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
        pattern             TEXT    NOT NULL CHECK(pattern IN ('A', 'B')),
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
        report_type  TEXT    NOT NULL CHECK(report_type IN ('morning', 'evening', 'monthly')),
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
        pattern          TEXT    CHECK(pattern IN ('A', 'B', NULL)),
        entry_price      REAL    NOT NULL,
        stop_loss_price  REAL    NOT NULL,
        target_price     REAL    NOT NULL,
        position_size    REAL,
        exit_price       REAL,
        exit_date        DATE,
        exit_reason      TEXT    CHECK(exit_reason IN ('stop_loss', 'take_profit', 'manual', NULL)),
        pnl_pct          REAL,
        pnl_amount       REAL,
        status           TEXT    NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'closed')),
        created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_paper_trades_status ON paper_trades(status)",
    "CREATE INDEX IF NOT EXISTS idx_paper_trades_ticker ON paper_trades(ticker, status)",
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
                logger.error(f"  DDL実行エラー（ステップ{i}）: {e}\n  SQL: {stmt[:80]}...")
                conn.close()
                raise

        _run_migrations(conn)
        conn.commit()
        logger.info(f"DB初期化完了: テーブル {success_count} 件を作成（または確認）しました。")

    finally:
        conn.close()

    _print_table_summary(db_path)


def _run_migrations(conn: sqlite3.Connection) -> None:
    """
    既存DBに対してカラム追加などのマイグレーションを実行する。
    カラムが既に存在する場合は OperationalError を無視する（冪等）。
    """
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
