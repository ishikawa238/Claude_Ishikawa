# 投資判断支援システム 詳細設計書 v1.5

## 1. 文書概要

本書は、要件定義書 v2.0 をもとに、DB定義・バッチフロー設計・Slack通知テンプレート・ディレクトリ構成・設定ファイル仕様を定義する。

### 1.1 変更履歴

#### v1.5（2026-04-19）

- 半自動注文機能を削除し、moomoo連携を保有同期のみに縮小
  - `src/order_executor.py`, `run_order_server.bat` を削除
  - `src/notifier.py` から注文リンク生成コードを除去
  - `scripts/order_server.py` を注文確認機能なしのレポートサーバーに変更（`/report`, `/health` のみ）
  - `config.json` の `order_server` セクションを削除し `report_server` に置き換え。`moomoo` セクションは保有同期用に host/port のみ残存（任意）
  - セクション9（moomoo OpenAPI 連携）・セクション10（半自動注文機能）を削除
  - `src/moomoo_sync.py` を新設: moomoo OpenD からの保有銘柄同期 + 約定履歴取得（バッチで自動実行、未接続時はスキップ）
- Slack通知のバッチを毎日実行（平日のみから変更）
  - タスクスケジューラ設定を月〜日（全曜日）20:30/07:30 に更新済み

#### v1.4（2026-04-16）

- 通知内容拡充・日次HTMLレポートページを追加（セクション11）
  - 買い候補ブロックにランク・RSI実値・出来高倍率・損切り→利確1行表示を追加
  - 保有銘柄ブロックに保有日数・利確目安までの距離を追加
  - 朝夜通知フッターに `/report` リンクを追加
  - `order_server.py` に `/report` エンドポイント（保有銘柄・シグナル履歴・市場環境）を追加
  - 修正対象ファイル: `src/entry_calculator.py`, `src/holdings_monitor.py`, `src/notifier.py`, `scripts/order_server.py`
  - テスト追加: `tests/test_notifier.py`（16テスト全パス）

#### v1.3（2026-04-15）

- 保有銘柄取得の不具合を調査・修正（セクション10を追加）
  - holdings テーブルの重複行蓄積バグを修正（NVDA/GOOG が過大表示されていた原因）
  - ETF（VT等）がスキップされる問題を修正（price_history 欠落の問題）
  - 修正対象ファイル: `src/holdings_monitor.py`, `scripts/init_db.py`, `scripts/batch_*.py`
  - テスト追加: `tests/test_holdings_monitor.py`（14テスト全パス）

#### v1.2（2026-04-11）

- 初回セットアップフロー（yfinance による3年分履歴一括取得）を追記
- ディレクトリ構成を実装に合わせて更新（実行用 .bat / .ps1 / SETUP.md 等）
- バッチスケジュール時刻を実装に合わせて修正（20:30 / 07:30 起動）
- 未解決事項の解決済み項目を更新

---

## 2. DB定義

### 2.1 設計方針

- SQLite 3 を使用する
- すべてのテーブルに `created_at` を持たせる
- 外部キー制約は有効化する（`PRAGMA foreign_keys = ON`）
- 日付型は `DATE`（YYYY-MM-DD）、日時型は `DATETIME`（YYYY-MM-DD HH:MM:SS）で統一する
- 数値はすべて `REAL`（浮動小数点）を使用する（整数でも将来の小数対応のため）
- フラグ系は `INTEGER`（0 / 1）で管理する

---

### 2.2 テーブル一覧

| テーブル名 | 用途 |
|-----------|------|
| universe_master | 銘柄マスタ |
| price_history | 日足価格履歴 |
| technical_snapshot | 日次テクニカル指標 |
| index_snapshot | 市場インデックスの日次データ |
| market_environment | 日次市場環境モード |
| holdings | 保有銘柄管理 |
| daily_signals | 日次判定結果 |
| trade_signal_log | シグナル発信履歴（エントリーセット） |
| trade_result_log | 実際のトレード結果（手動入力） |
| report_log | 通知本文履歴 |
| error_log | エラーログ |
| batch_run_log | バッチ実行ログ |

---

### 2.3 DDL

```sql
-- 外部キー制約を有効化（接続のたびに実行すること）
PRAGMA foreign_keys = ON;

-- ============================================================
-- 銘柄マスタ
-- ============================================================
CREATE TABLE IF NOT EXISTS universe_master (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT    NOT NULL,
    name                TEXT    NOT NULL,
    market              TEXT    NOT NULL CHECK(market IN ('JP', 'US')),
    exchange            TEXT,
    shares_outstanding  REAL,                        -- 発行済株式数
    is_active           INTEGER NOT NULL DEFAULT 1,  -- 1: 取引中, 0: 上場廃止等
    is_excluded         INTEGER NOT NULL DEFAULT 0,  -- 1: 対象外（ETF・REITなど）
    exclude_reason      TEXT,                        -- 除外理由
    last_updated        DATE    NOT NULL,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_universe_master_ticker
    ON universe_master(ticker);
CREATE INDEX IF NOT EXISTS idx_universe_master_market
    ON universe_master(market, is_active, is_excluded);

-- ============================================================
-- 日足価格履歴
-- ============================================================
CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT    NOT NULL,
    date        DATE    NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    adj_close   REAL,    -- 修正後終値（指標計算に使用）
    volume      REAL,
    is_holiday  INTEGER NOT NULL DEFAULT 0,  -- 1: 市場休場日
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (ticker) REFERENCES universe_master(ticker)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_price_history_ticker_date
    ON price_history(ticker, date);
CREATE INDEX IF NOT EXISTS idx_price_history_date
    ON price_history(date);

-- ============================================================
-- 日次テクニカル指標
-- ============================================================
CREATE TABLE IF NOT EXISTS technical_snapshot (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    date            DATE    NOT NULL,
    -- 移動平均
    ma5             REAL,
    ma25            REAL,
    ma75            REAL,
    ma200           REAL,
    ma5_5d_ago      REAL,   -- 5日前のMA5（傾き判定用）
    ma25_5d_ago     REAL,   -- 5日前のMA25（傾き判定用）
    ma200_5d_ago    REAL,   -- 5日前のMA200（傾き判定用）
    -- 出来高・高値
    vol_20d_avg     REAL,   -- 20日平均出来高
    high_20d        REAL,   -- 20日高値（終値ベース）
    high_52w        REAL,   -- 52週高値（終値ベース）
    low_52w         REAL,   -- 52週安値（終値ベース）
    -- 騰落率
    return_5d       REAL,   -- 5日騰落率（%）
    return_20d      REAL,   -- 20日騰落率（%）
    -- オシレーター
    rsi14           REAL,   -- RSI（14日・Wilder）
    atr14           REAL,   -- ATR（14日）
    volatility_20d  REAL,   -- 20日ボラティリティ（年率換算・%）
    -- 推計値
    est_turnover    REAL,   -- 推計売買代金（adj_close × volume）
    est_market_cap  REAL,   -- 推計時価総額（adj_close × shares_outstanding）
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (ticker) REFERENCES universe_master(ticker)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_technical_snapshot_ticker_date
    ON technical_snapshot(ticker, date);
CREATE INDEX IF NOT EXISTS idx_technical_snapshot_date
    ON technical_snapshot(date);

-- ============================================================
-- 市場インデックス日次データ
-- ============================================================
CREATE TABLE IF NOT EXISTS index_snapshot (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    index_code  TEXT    NOT NULL,   -- N225, TOPIX, SP500, NASDAQ
    date        DATE    NOT NULL,
    close       REAL    NOT NULL,
    ma25        REAL,
    ma75        REAL,
    ma200       REAL,
    ma25_5d_ago REAL,   -- 傾き判定用
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_index_snapshot_code_date
    ON index_snapshot(index_code, date);

-- ============================================================
-- 市場環境モード
-- ============================================================
CREATE TABLE IF NOT EXISTS market_environment (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            DATE    NOT NULL UNIQUE,
    jp_above_ma25   INTEGER,  -- 1: 日経平均がMA25以上, 0: 未満, NULL: データなし
    us_above_ma25   INTEGER,  -- 1: S&P500がMA25以上, 0: 未満, NULL: データなし
    mode            TEXT    NOT NULL CHECK(mode IN ('normal', 'caution', 'full_caution')),
    n225_close      REAL,
    n225_ma25       REAL,
    sp500_close     REAL,
    sp500_ma25      REAL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- 保有銘柄
-- ============================================================
CREATE TABLE IF NOT EXISTS holdings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker           TEXT    NOT NULL,
    name             TEXT,
    market           TEXT    NOT NULL CHECK(market IN ('JP', 'US')),
    entry_price      REAL    NOT NULL,
    entry_date       DATE    NOT NULL,
    quantity         REAL,                -- 保有数量（株数・単元数）
    stop_loss_price  REAL,                -- NULLの場合はentry_price × 0.92を使用
    memo             TEXT,
    is_closed        INTEGER NOT NULL DEFAULT 0,  -- 1: 決済済み
    close_price      REAL,
    close_date       DATE,
    close_reason     TEXT CHECK(close_reason IN ('stop_loss', 'take_profit', 'manual', NULL)),
    created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_holdings_active
    ON holdings(is_closed, ticker);

-- ============================================================
-- 日次シグナル（判定結果）
-- ============================================================
CREATE TABLE IF NOT EXISTS daily_signals (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker                  TEXT    NOT NULL,
    date                    DATE    NOT NULL,
    market                  TEXT    NOT NULL CHECK(market IN ('JP', 'US')),
    pattern                 TEXT    CHECK(pattern IN ('A', 'B', NULL)),
    label                   TEXT    NOT NULL CHECK(label IN ('candidate', 'watch', 'skip')),
    -- パターンA条件充足フラグ（1: 充足, 0: 未充足, NULL: 対象外）
    cond_a_ma200_rising     INTEGER,
    cond_a_ma_aligned       INTEGER,
    cond_a_52w_high_near    INTEGER,
    cond_a_volume_surge     INTEGER,
    cond_a_rsi_ok           INTEGER,
    cond_a_above_ma25       INTEGER,
    -- パターンB条件充足フラグ
    cond_b_ma200_rising     INTEGER,
    cond_b_ma_aligned       INTEGER,
    cond_b_pullback_touched INTEGER,
    cond_b_bounced          INTEGER,
    cond_b_ma25_rising      INTEGER,
    cond_b_rsi_ok           INTEGER,
    -- 共通指標スナップショット（通知用）
    close                   REAL,
    rsi14                   REAL,
    atr14                   REAL,
    vol_ratio               REAL,   -- 当日出来高 ÷ 20日平均出来高
    rs_rank                 INTEGER,  -- 相対強度ランク（1が最強）
    created_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (ticker) REFERENCES universe_master(ticker)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_signals_ticker_date
    ON daily_signals(ticker, date);
CREATE INDEX IF NOT EXISTS idx_daily_signals_date_label
    ON daily_signals(date, label);

-- ============================================================
-- シグナル発信ログ（エントリーセット）
-- ============================================================
CREATE TABLE IF NOT EXISTS trade_signal_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_date         DATE    NOT NULL,
    ticker              TEXT    NOT NULL,
    name                TEXT,
    market              TEXT    NOT NULL CHECK(market IN ('JP', 'US')),
    pattern             TEXT    NOT NULL CHECK(pattern IN ('A', 'B')),
    entry_price         REAL    NOT NULL,   -- 翌日寄付目安
    stop_loss_price     REAL    NOT NULL,
    stop_loss_pct       REAL    NOT NULL,   -- 損切り幅（%）
    target_price        REAL    NOT NULL,   -- 利確目安（リスクリワード2:1）
    recommended_amount  REAL,              -- 推奨投入額（円 or ドル）
    market_mode         TEXT    NOT NULL,   -- シグナル発信時の市場環境モード
    conditions_met      TEXT,              -- 充足した条件のJSON文字列
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_trade_signal_log_date
    ON trade_signal_log(signal_date);
CREATE INDEX IF NOT EXISTS idx_trade_signal_log_ticker
    ON trade_signal_log(ticker);

-- ============================================================
-- トレード実績ログ（手動入力）
-- ============================================================
CREATE TABLE IF NOT EXISTS trade_result_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id           INTEGER NOT NULL,
    ticker              TEXT    NOT NULL,
    actual_entry_price  REAL,               -- 実際のエントリー価格
    actual_entry_date   DATE,
    actual_exit_price   REAL,
    actual_exit_date    DATE,
    exit_reason         TEXT CHECK(exit_reason IN ('stop_loss', 'take_profit', 'manual', 'forced_stop', NULL)),
    pnl_pct             REAL,               -- 損益率（%）。システムが自動計算
    pnl_amount          REAL,               -- 損益額（円 or ドル）
    memo                TEXT,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (signal_id) REFERENCES trade_signal_log(id)
);
CREATE INDEX IF NOT EXISTS idx_trade_result_log_signal
    ON trade_result_log(signal_id);

-- ============================================================
-- 通知本文ログ
-- ============================================================
CREATE TABLE IF NOT EXISTS report_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date  DATE    NOT NULL,
    report_type  TEXT    NOT NULL CHECK(report_type IN ('morning', 'evening', 'monthly')),
    content      TEXT    NOT NULL,   -- 通知本文（JSON or プレーンテキスト）
    slack_status TEXT    CHECK(slack_status IN ('success', 'failed', 'skipped')),
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_report_log_date
    ON report_log(report_date, report_type);

-- ============================================================
-- エラーログ
-- ============================================================
CREATE TABLE IF NOT EXISTS error_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    error_time    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    batch_type    TEXT,     -- evening_jp / morning_all / monthly
    module        TEXT,     -- data_fetcher / technical_calculator 等
    ticker        TEXT,     -- 対象銘柄（あれば）
    error_type    TEXT,     -- APIError / DBError / CalcError 等
    error_message TEXT,
    is_fatal      INTEGER NOT NULL DEFAULT 0   -- 1: バッチ全体に影響する致命的エラー
);
CREATE INDEX IF NOT EXISTS idx_error_log_time
    ON error_log(error_time);

-- ============================================================
-- バッチ実行ログ
-- ============================================================
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
);
CREATE INDEX IF NOT EXISTS idx_batch_run_log_date
    ON batch_run_log(run_date, batch_type);
```

---

### 2.4 インデックス設計の補足

| インデックス | 対象クエリ |
|-------------|-----------|
| `price_history(ticker, date)` UNIQUE | 日足取得・重複防止 |
| `price_history(date)` | 特定日の全銘柄取得（テクニカル計算時） |
| `technical_snapshot(ticker, date)` UNIQUE | 指標取得・重複防止 |
| `technical_snapshot(date)` | 特定日のスクリーニング |
| `daily_signals(date, label)` | 候補抽出クエリの高速化 |
| `trade_signal_log(signal_date)` | 月次集計クエリ |

---

## 3. バッチフロー設計

### 3.1 全体スケジュール

```
毎日（日本時間）
20:30 ──── [夜バッチ：日本株] ─────────────────────────────────
             └─ 日本株価格取得・テクニカル計算・シグナル検出
             └─ 夜通知（Slack）送信

07:30 ──── [朝バッチ：米国株含む全体] ──────────────────────────
             └─ 米国株価格取得・テクニカル計算・シグナル検出
             └─ 朝通知（Slack）送信

毎月1日 08:00 ─ [月次バッチ（タスクスケジューラ or 手動）] ─────
             └─ 銘柄マスタ更新・月次集計レポート生成
```

※ Windows タスクスケジューラで自動実行。`setup_scheduler.ps1` で一括登録可能。

---

### 3.2 夜バッチ（evening_jp）詳細フロー

```
START
│
├─ [1] batch_run_log: status='running' で登録
│
├─ [2] 価格データ取得（data_fetcher）
│   ├─ EODHD Bulk EOD endpoint: 日本株全銘柄の日足を1リクエストで取得
│   │   └─ `fetch_bulk_eod("JP", target_date)` → eod/bulk_last_day/JP
│   ├─ EODHD API: 日本株インデックス（N225, TOPIX）を取得
│   ├─ 失敗時: 最大3回リトライ → 失敗なら error_log 記録・スキップ
│   └─ price_history / index_snapshot に UPSERT
│
├─ [3] テクニカル指標計算（technical_calculator）
│   ├─ price_history から直近756日分を読み込み
│   ├─ MA5/25/75/200・傾き用5日前MA・RSI14・ATR14・ボラティリティ・出来高比等を計算
│   ├─ 計算に必要なデータ不足の場合は全フィールドNULL
│   └─ technical_snapshot に UPSERT
│
├─ [4] 市場環境判定（market_filter）
│   ├─ index_snapshot から N225・TOPIX の最新MA25を取得
│   ├─ N225終値 >= N225_MA25 → jp_above_ma25 = 1
│   ├─ mode判定:
│   │   jp=1: normal（米国は翌朝確定のため日本株夜バッチでは jp のみ判定）
│   │   jp=0: caution
│   └─ market_environment に UPSERT
│
├─ [5] 流動性フィルタ（market_filter）
│   ├─ 日本株フィルタ適用（株価300円以上・推計売買代金3億以上・推計時価総額300億以上）
│   ├─ 共通フィルタ適用（非アクティブ除外・MA200計算可能銘柄のみ）
│   └─ 通過銘柄リストをメモリ上に保持
│
├─ [6] 売買パターン検出（signal_detector）
│   ├─ market_environment.mode を確認
│   │   └─ caution / full_caution の場合: label='skip' で記録してステップ[6]終了
│   ├─ 通過銘柄に対してパターンA・B の条件を評価
│   ├─ 全条件充足 → label='candidate'
│   ├─ 1〜2条件未充足 → label='watch'
│   ├─ それ以外 → label='skip'（daily_signalsには保存しない）
│   ├─ vol_ratio（出来高比）の降順でランク付け
│   └─ daily_signals に INSERT
│
├─ [7] エントリーセット計算（entry_calculator）
│   ├─ label='candidate' の銘柄のみ処理
│   ├─ エントリー価格 = 当日終値（翌日寄付想定）
│   ├─ 損切りライン = entry_price - (ATR14 × 2)
│   ├─ 損切り幅(%) = (entry - stop) / entry × 100
│   ├─ 利確目安 = entry × (1 + stop_loss_pct × 2)
│   ├─ 推奨投入額 = total_capital × 0.02 / stop_loss_pct
│   ├─ 警告チェック:
│   │   └─ 損切り幅 < 1% → 警告フラグ
│   │   └─ 損切り幅 > 15% → 警告フラグ
│   └─ trade_signal_log に INSERT
│
├─ [8] 保有銘柄判定（holdings_monitor）
│   ├─ holdings.csv を読み込み → holdings テーブルに同期
│   ├─ 各保有銘柄の当日終値・損益率を計算
│   ├─ 強制損切りチェック（終値 <= stop_loss_price）
│   ├─ 売り検討チェック（MA5下抜け・MA25を5%下回る等）
│   └─ 判定結果をメモリ上に保持（通知生成に使用）
│
├─ [9] 夜通知生成・送信（notifier）
│   ├─ Slack Block Kit JSON を組み立て
│   ├─ Slack Webhook に POST
│   ├─ 成功/失敗を report_log に記録
│   └─ 失敗時は error_log に記録（バッチは継続）
│
├─ [10] DBバックアップ（data_store）
│   ├─ ./data/backup/investment_YYYYMMDD.db にコピー
│   ├─ 8日以前のバックアップを削除（直近7日分を保持）
│   └─ 失敗時は error_log に記録（バッチは継続）
│
└─ [11] batch_run_log: status を success / partial_failure / failed で更新
END
```

---

### 3.3 朝バッチ（morning_all）詳細フロー

```
START（07:30）
│
├─ [1] batch_run_log: status='running' で登録
│
├─ [2] 米国株価格データ取得（data_fetcher）
│   ├─ EODHD Bulk EOD endpoint: 米国株全銘柄の日足を1リクエストで取得
│   │   └─ `fetch_bulk_eod("US", target_date)` → eod/bulk_last_day/US
│   ├─ EODHD API: 米国株インデックス（S&P500, NASDAQ）を取得
│   └─ price_history / index_snapshot に UPSERT
│
├─ [3] テクニカル指標計算（technical_calculator）
│   └─ 米国株分のみ計算・technical_snapshot に UPSERT
│
├─ [4] 市場環境モード更新（market_filter）
│   ├─ 前夜バッチの jp_above_ma25 を取得
│   ├─ 米国株インデックスで us_above_ma25 を判定
│   ├─ 最終モード確定:
│   │   jp=1 かつ us=1 → normal
│   │   jp=1 または us=1 → caution
│   │   jp=0 かつ us=0 → full_caution
│   └─ market_environment を UPDATE
│
├─ [5] 米国株: 流動性フィルタ・パターン検出・エントリーセット計算
│   └─ 夜バッチ [5][6][7] と同様の処理を米国株に対して実行
│
├─ [6] 保有銘柄判定（米国株分）
│   └─ 夜バッチ [8] と同様の処理を米国株保有銘柄に対して実行
│
├─ [7] 朝通知生成・送信（notifier）
│   ├─ 強制損切り対象（日本株 + 米国株）を最優先で取得
│   ├─ 市場環境モードを確定値で組み込み
│   ├─ 買い候補を上位2件まで取得（日本株は前夜バッチ結果、米国株は当バッチ結果）
│   ├─ Slack Block Kit JSON を組み立て
│   ├─ Slack Webhook に POST
│   └─ report_log に記録
│
└─ [8] batch_run_log 更新
END
```

---

### 3.4 月次バッチ（monthly）詳細フロー

```
START（毎月第1営業日・手動実行 or タスクスケジューラ）
│
├─ [1] 銘柄マスタ更新（data_fetcher）
│   ├─ EODHD API: 日本株・米国株の銘柄リストを全件取得
│   ├─ 新規銘柄を universe_master に INSERT
│   ├─ 上場廃止銘柄の is_active を 0 に更新
│   └─ last_updated を更新
│
├─ [2] 月次集計レポート生成（report_generator）
│   ├─ 集計対象期間: 前月1日〜末日
│   ├─ trade_signal_log / trade_result_log から集計
│   ├─ 指標計算:
│   │   シグナル数・実行数・実行率
│   │   勝率・平均利益・平均損失・期待値
│   │   パターン別勝率（A / B）
│   │   市場別勝率（JP / US）
│   ├─ サンプル数 < 10 の場合は警告メッセージを付記
│   └─ Slack に送信 + report_log に保存
│
└─ [3] batch_run_log 更新
END
```

---

### 3.5 エラーハンドリング方針

| エラー種別 | 対応 | バッチへの影響 |
|-----------|------|--------------|
| 特定銘柄のAPIタイムアウト | 3回リトライ後スキップ | 継続 |
| 全銘柄のAPI失敗（キー無効等） | 即座にfailed記録 | 停止 |
| テクニカル計算エラー（1銘柄） | スキップ・error_log記録 | 継続 |
| DBへの書き込み失敗 | 3回リトライ後停止 | 停止 |
| Slack通知送信失敗 | error_log記録 | 継続（バッチは止めない） |
| DBバックアップ失敗 | error_log記録 | 継続 |

---

## 4. Slack通知テンプレート

### 4.1 Slack Block Kit 設計方針

- Slack Incoming Webhooks を使用する
- Block Kit の `blocks` 配列で構造化する
- 危険度に応じてカラーを使い分ける
  - 強制損切り：`danger`（赤）
  - 警告：`warning`（黄）
  - 通常：`good`（緑）または デフォルト
- 1メッセージは1000文字以内を目標とする（Slackの表示崩れ防止）

---

### 4.2 朝通知テンプレート

#### 通常モード・損切り対象あり

```
╔══════════════════════════════════════╗
  📅 朝の投資チェック｜2025-01-20 (月)
╚══════════════════════════════════════╝

🚨 【要即時対応】損切り対象（1件）

  7203 トヨタ自動車 (JP)
  終値: 2,944円｜損切りライン: 2,950円
  損益率: -8.3%（損失額: -8,300円推計）
  ▶ 本日寄付での手仕舞いを推奨します

──────────────────────────────────────

📊 市場環境：通常モード
  日経平均 38,500円（MA25比: +1.2%↑）
  S&P500   4,820（MA25比: +0.8%↑）

──────────────────────────────────────

🟢 本日の買い候補（2件）

  [A] 6758 ソニーグループ (JP)
  パターン：モメンタムブレイクアウト
  エントリー目安：13,200円
  損切りライン：12,584円（-4.7%）
  利確目安：14,432円（+9.3%）
  推奨投入額：84,000円（総資金2%リスク）

  [B] NVDA NVIDIA Corp (US)
  パターン：押し目買い
  エントリー目安：$620
  損切りライン：$572（-7.7%）
  利確目安：$716（+15.5%）
  推奨投入額：$519（総資金2%リスク）

──────────────────────────────────────

📋 保有銘柄サマリー
  継続保有: 2件｜売り検討: 1件｜強制損切り: 1件
  ※詳細は夜通知を確認してください
```

#### 警戒モード

```
╔══════════════════════════════════════╗
  📅 朝の投資チェック｜2025-01-21 (火)
╚══════════════════════════════════════╝

⚠️ 市場環境：警戒モード
  S&P500が25日線を下回っています
  S&P500 4,690（MA25比: -1.5%↓）
  日経平均 38,200円（MA25比: +0.3%↑）

  ⛔ 本日は新規買いシグナルを停止しています
  保有銘柄の損切りラインに注意してください

──────────────────────────────────────

📋 保有銘柄サマリー
  継続保有: 3件｜売り検討: 0件｜損切り接近: 1件

  ⚠️ 損切りライン接近
  AAPL Apple Inc (US)
  終値: $177.2｜損切りライン: $174.0（残り-1.8%）
```

---

### 4.3 夜通知テンプレート

#### 通常モード・買い候補あり

```
╔══════════════════════════════════════╗
  🌙 夜の投資レポート｜2025-01-20 (月)
╚══════════════════════════════════════╝

📊 市場環境確定値：通常モード
  日経平均 38,512円（前日比 +0.4%）MA25比 +1.3%↑
  TOPIX   2,680（前日比 +0.2%）
  S&P500  4,823（前日比 +0.6%）MA25比 +0.9%↑
  NASDAQ  15,210（前日比 +0.8%）

──────────────────────────────────────

🟢 新規買い候補（3件）

━━ No.1 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  6758 ソニーグループ (JP)
  パターン：A（モメンタムブレイクアウト）
  終値: 13,200円｜52週高値: 13,150円（更新）
  出来高比: 2.3倍｜RSI: 63

  エントリー目安：13,200円（翌日寄付）
  損切りライン：12,584円（-4.7%）[ATR×2]
  利確目安：14,432円（+9.3%）[RR=2:1]
  推奨投入額：84,000円

  ✅ MA200上昇中
  ✅ MA75>MA200, MA25>MA75, MA5>MA25
  ✅ 52週高値更新（95%以上）
  ✅ 出来高急増（20日比2.3倍）
  ✅ RSI 63（適正）

━━ No.2 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  4063 信越化学工業 (JP)
  パターン：B（押し目買い）
  終値: 5,820円｜MA25: 5,791円
  出来高比: 1.1倍｜RSI: 52

  エントリー目安：5,820円（翌日寄付）
  損切りライン：5,534円（-4.9%）[ATR×2]
  利確目安：6,394円（+9.9%）[RR=2:1]
  推奨投入額：81,600円

  ✅ MA200上昇中
  ✅ MA整列確認
  ✅ MA25タッチ後反発確認（3日前）
  ✅ MA25上昇中
  ✅ RSI 52（適正）

━━ No.3 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  NVDA NVIDIA Corp (US)
  パターン：B（押し目買い）
  終値: $620｜MA25: $611
  出来高比: 0.9倍｜RSI: 55

  エントリー目安：$620（翌日寄付）
  損切りライン：$572（-7.7%）[ATR×2]
  利確目安：$716（+15.5%）[RR=2:1]
  推奨投入額：$519

  ⚠️ 損切り幅が広めです（7.7%）。ボラティリティに注意

──────────────────────────────────────

📋 保有銘柄全件チェック

  7203 トヨタ自動車 (JP)
  ラベル：🔴 強制損切り
  終値: 2,944円｜取得単価: 3,200円
  損益率: -8.0%（-8,000円推計）
  損切りライン: 2,950円（割込み）
  理由：終値が損切りラインを下回りました

  6861 キーエンス (JP)
  ラベル：🟡 売り検討
  終値: 65,200円｜取得単価: 62,000円
  損益率: +5.2%（+3,200円推計）
  理由：MA5がMA25を下抜けました

  AAPL Apple Inc (US)
  ラベル：🟢 継続保有
  終値: $177.2｜取得単価: $172.0
  損益率: +3.0%
  次の確認ポイント：MA25（$174.0）を維持できるか

──────────────────────────────────────

🔭 明日の注目ポイント
  ・7203 トヨタ：損切りライン割込み → 寄付での手仕舞い推奨
  ・6861 キーエンス：MA5<MA25 継続するか確認
  ・データ取得失敗：2件（error_logを確認してください）
```

---

### 4.4 月次集計レポートテンプレート

```
╔══════════════════════════════════════╗
  📈 月次集計レポート｜2025年1月
╚══════════════════════════════════════╝

📊 全体サマリー
  シグナル数: 18件
  実行数: 8件（実行率: 44%）
  決済済み: 6件

  勝率: 50.0%（3勝 / 3敗）
  平均利益: +8.2%
  平均損失: -5.1%
  期待値: +1.0% / トレード ✅

  ※ サンプル数6件（統計的信頼性は低めです。継続記録を推奨）

──────────────────────────────────────

📊 パターン別
  パターンA（ブレイクアウト）: 4件 勝率50% 期待値+1.2%
  パターンB（押し目買い）:    2件 勝率50% 期待値+0.7%

📊 市場別
  日本株: 4件 勝率50% 平均損益+1.5%
  米国株: 2件 勝率50% 平均損益+0.3%

──────────────────────────────────────

💰 損益概算
  利益確定: +49,200円
  損失確定: -30,600円
  純益: +18,600円
  システムコスト（推計）: -4,000円
  差引: +14,600円

──────────────────────────────────────

📌 来月の改善メモ（手動記入欄）
  自由記述
```

---

## 5. ディレクトリ構成

```
investment_support/
│
├── config.json               # 設定ファイル（APIキー・総資金・Slack URL等）※Git管理外
├── config_template.json      # 設定ファイルのテンプレート（APIキーは空欄）
├── holdings.csv              # 保有銘柄管理ファイル（手動更新）
├── requirements.txt          # Python依存パッケージ一覧
├── SETUP.md                  # セットアップ手順書
│
├── run_evening_jp.bat        # 夜バッチ起動用バッチファイル
├── run_morning_all.bat       # 朝バッチ起動用バッチファイル
├── run_monthly.bat           # 月次バッチ起動用バッチファイル
├── setup_scheduler.ps1       # Windowsタスクスケジューラ一括登録スクリプト
│
├── data/
│   ├── investment.db         # SQLiteデータベース本体
│   └── backup/               # DBバックアップ（investment_YYYYMMDD.db）
│
├── logs/
│   └── batch_YYYYMMDD.log    # バッチ実行ログ（日付別）
│
├── src/
│   ├── __init__.py
│   ├── config.py             # 設定ファイル読み込み（CONFIG グローバル変数）
│   ├── logger.py             # 統一ロギング設定（コンソール + ファイル出力）
│   ├── data_fetcher.py       # EODHDからのデータ取得（Bulk EOD / 個別 / インデックス）
│   ├── yfinance_fetcher.py   # yfinance による初回3年分履歴取得（無料・APIキー不要）
│   ├── data_store.py         # SQLiteへの保存・バックアップ
│   ├── technical_calculator.py  # テクニカル指標計算
│   ├── market_filter.py      # 流動性フィルタ・市場環境判定
│   ├── signal_detector.py    # パターンA・B の検出ロジック
│   ├── entry_calculator.py   # エントリーセット計算・ポジションサイジング
│   ├── holdings_monitor.py   # 保有銘柄の損益計算・損切り判定
│   ├── notifier.py           # Slack Block Kit 生成・送信
│   ├── moomoo_sync.py        # moomoo OpenD からの保有銘柄同期・約定履歴取得（任意）
│   └── report_generator.py   # 月次集計レポート生成
│
├── scripts/
│   ├── init_db.py            # DB初期化（DDL実行）
│   ├── fetch_historical.py   # 初回3年分履歴データ一括取得（yfinance使用）
│   ├── order_server.py       # 日次レポート用ローカルWebサーバー（Flask）
│   ├── batch_evening_jp.py   # 夜バッチ エントリーポイント
│   ├── batch_morning_all.py  # 朝バッチ エントリーポイント
│   ├── batch_monthly.py      # 月次バッチ エントリーポイント
│   └── add_trade_result.py   # トレード結果の手動入力ツール
│
└── tests/
    ├── __init__.py
    ├── test_holdings_monitor.py
    └── test_notifier.py
```

---

## 6. 設定ファイル仕様（config.json）

```json
{
  "_comment": "このファイルにはAPIキーが含まれます。Gitにコミットしないでください",

  "eodhd": {
    "api_key": "YOUR_EODHD_API_KEY",
    "base_url": "https://eodhd.com/api",
    "request_interval_sec": 0.5
  },

  "slack": {
    "webhook_url": "https://hooks.slack.com/services/XXXXX/YYYYY/ZZZZZ"
  },

  "capital": {
    "total_capital_jpy": 200000,
    "total_capital_usd": 1500,
    "max_risk_per_trade_pct": 2.0,
    "max_concurrent_holdings": 3,
    "default_stop_loss_pct": 8.0
  },

  "signal": {
    "pattern_a_rsi_min": 50,
    "pattern_a_rsi_max": 70,
    "pattern_a_volume_ratio_min": 1.5,
    "pattern_a_52w_high_pct": 0.95,
    "pattern_b_rsi_min": 40,
    "pattern_b_rsi_max": 60,
    "pattern_b_ma25_touch_pct": 0.98,
    "pattern_b_lookback_days": 5,
    "atr_stop_multiplier": 2.0,
    "risk_reward_ratio": 2.0,
    "max_entry_sets_per_batch": 3,
    "stop_loss_warning_min_pct": 1.0,
    "stop_loss_warning_max_pct": 15.0
  },

  "screening": {
    "jp_min_price": 300,
    "jp_min_turnover": 300000000,
    "jp_min_market_cap": 30000000000,
    "us_min_price": 5,
    "us_min_turnover": 20000000,
    "us_min_market_cap": 2000000000,
    "min_active_days_of_30": 20
  },

  "schedule": {
    "evening_jp_target_time": "20:30",
    "morning_all_target_time": "07:30"
  },

  "database": {
    "db_path": "./data/investment.db",
    "backup_dir": "./data/backup",
    "backup_retention_days": 7
  },

  "logging": {
    "log_dir": "./logs",
    "log_level": "INFO"
  },

  "initial_fetch": {
    "history_years": 3
  },

  "report_server": {
    "host": "127.0.0.1",
    "port": 5100
  },

  "moomoo": {
    "host": "127.0.0.1",
    "port": 11111
  }
}
```

`moomoo` セクションは任意。moomoo OpenD から保有銘柄を同期する場合のみ設定する。未設定でも `holdings.csv` による管理で動作する。

### 6.1 report_server セクション仕様

| キー | 型 | 必須 | 説明 |
|------|-----|------|------|
| host | TEXT | ✅ | Web サーバーのリスンホスト（必ず 127.0.0.1） |
| port | INTEGER | ✅ | Web サーバーのポート（デフォルト 5100） |

Slack 通知フッターの「詳細レポートを見る」リンクの接続先。`python -m scripts.order_server` で起動する。

---

### 6.2 holdings.csv フォーマット仕様

```csv
ticker,name,market,entry_price,entry_date,quantity,stop_loss_price,memo
7203,トヨタ自動車,JP,3200,2025-01-15,10,,
6758,ソニーグループ,JP,13200,2025-01-20,1,12584,ブレイクアウト後エントリー
AAPL,Apple Inc,US,185.5,2025-01-10,5,170.0,
NVDA,NVIDIA Corp,US,620,2025-01-20,2,,
```

| 列名 | 型 | 必須 | 説明 |
|-----|-----|------|------|
| ticker | TEXT | ✅ | 銘柄コード（JP: 数字4桁、US: アルファベット） |
| name | TEXT | | 銘柄名（省略可） |
| market | TEXT | ✅ | JP または US |
| entry_price | REAL | ✅ | 取得単価 |
| entry_date | DATE | ✅ | 取得日（YYYY-MM-DD） |
| quantity | REAL | | 保有数量（省略可） |
| stop_loss_price | REAL | | 損切りライン。省略時は entry_price × 0.92 |
| memo | TEXT | | メモ（省略可） |

---

## 7. 主要な処理の実装メモ

### 7.1 Wilder RSI の計算手順

```
1. 終値の1日変化分を計算: delta = adj_close[t] - adj_close[t-1]
2. 上昇分: gain = max(delta, 0)、下降分: loss = max(-delta, 0)
3. 初回（14日目）の平均: avg_gain = sum(gain[1:14]) / 14
                          avg_loss = sum(loss[1:14]) / 14
4. 以降（15日目〜）: avg_gain = (avg_gain × 13 + gain[t]) / 14
                     avg_loss = (avg_loss × 13 + loss[t]) / 14
5. RS = avg_gain / avg_loss（avg_loss = 0 の場合 RS = 無限大 → RSI = 100）
6. RSI = 100 - 100 / (1 + RS)
```

### 7.2 パターンB「押し目タッチ」の判定手順

```
1. 直近 pattern_b_lookback_days 日間（デフォルト5日）の価格履歴を取得
2. 各日の安値 が MA25 × pattern_b_ma25_touch_pct（デフォルト0.98）以下であれば
   「押し目タッチあり」とフラグを立てる
3. 当日の終値 が MA25 以上であれば「反発確認」とする
4. 両方を満たせば cond_b_pullback_touched=1, cond_b_bounced=1
```

### 7.3 推奨投入額の計算（円・ドル混在）

```python
# 日本株の場合
stop_loss_pct = (entry_price - stop_loss_price) / entry_price
recommended_amount_jpy = total_capital_jpy * 0.02 / stop_loss_pct

# 米国株の場合
recommended_amount_usd = total_capital_usd * 0.02 / stop_loss_pct
```

---

## 8. 初回セットアップ手順

### 8.1 セットアップフロー

```
[1] 環境構築
    ├─ Python 3.11+ インストール
    ├─ 仮想環境作成: python -m venv .venv
    ├─ pip install -r requirements.txt
    └─ config_template.json → config.json にコピーし、APIキー等を設定

[2] DB初期化
    └─ python -m scripts.init_db
        → data/investment.db に全12テーブルを作成

[3] 初回履歴データ取得（数時間かかる）
    └─ python -m scripts.fetch_historical
        ├─ EODHD: 銘柄マスタ取得 → universe_master
        ├─ yfinance: 過去3年分の日足データ取得 → price_history
        │   └─ 50件ごとバッチ処理・APIキー不要（無料）
        ├─ EODHD: インデックス4本の過去3年分取得 → index_snapshot
        └─ テクニカルスナップショット計算（最終日のみ） → technical_snapshot

[4] タスクスケジューラ登録
    └─ PowerShell (管理者) で setup_scheduler.ps1 を実行
        → 夜バッチ(20:30)・朝バッチ(07:30)・月次バッチ(毎月1日 08:00) を登録（全曜日）
```

### 8.2 初回履歴取得の詳細（fetch_historical）

日次バッチの EODHD API 呼び出し数を節約するため、初回の大量履歴取得には yfinance（無料・APIキー不要）を使用する。

| データ | 取得元 | 対象 | 備考 |
|--------|--------|------|------|
| 銘柄マスタ | EODHD | JP + US 全銘柄 | exchange-symbol-list endpoint |
| 日足価格データ（3年分） | yfinance | 全銘柄 | 50件ずつバッチ処理 |
| インデックス（3年分） | EODHD | N225, TOPIX, SP500, NASDAQ | 4リクエスト |
| テクニカル指標 | ローカル計算 | 全銘柄（最終日のみ） | price_history から計算 |

※ 途中でエラーが出ても再実行可能（UPSERT で冪等）

---

## 9. 未解決事項

| 項目 | 内容 | 状態 |
|------|------|------|
| EODHD の発行済株式数の取得方法 | fundamental API が必要な可能性あり | 🔴 未解決。取得不可の場合は時価総額フィルタを一時的に無効化 |
| 日本株のティッカー形式 | EODHD では「7203.T」形式 | ✅ 解決済み。fetch_bulk_eod 内で `.T` / `.US` サフィックスを自動除去 |
| 米国株の市場休場日リスト | EODHD で休場日フラグが取れるか未確認 | 🔴 未解決。取れない場合は NYSE 休場日カレンダーをローカルで管理 |
| Windows タスクスケジューラの設定 | 具体的な登録手順が未定 | ✅ 解決済み。`setup_scheduler.ps1` で一括登録（管理者権限で実行） |

---

## 10. 保有銘柄取得不具合の調査・修正（v1.3）

### 10.1 問題の概要

バッチ実行ごとに NVDA・GOOG が Slack 通知で複数件表示される、
VT（Vanguard Total World ETF）等が通知に出てこない、という現象が確認された。

### 10.2 調査で判明した原因

#### 原因1: holdings テーブルに UNIQUE 制約なし → 重複行が蓄積

`holdings` テーブルの `ticker` 列に UNIQUE 制約が存在しなかった。

```sql
-- 修正前のテーブル定義（抜粋）
CREATE TABLE holdings (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT    NOT NULL,   -- UNIQUE 制約なし
    ...
)
```

`upsert()` 関数は `INSERT OR REPLACE` を使うが、REPLACE が機能するには
**UNIQUE 制約違反が発生する必要がある**。制約がないため毎回 INSERT になり、
バッチ実行のたびに同一銘柄の行が増殖していた。

**実際の重複状況（修正前）:**

| ticker | 行数 |
|--------|------|
| GOOG   | 6    |
| NVDA   | 6    |
| VT     | 6    |
| 計     | 18   |

`evaluate_holdings()` は `WHERE is_closed=0` で全行を取得するため、
同一銘柄が6件として処理・通知されていた。

#### 原因2: ETF（VT等）の price_history データ欠落

VT（Vanguard Total World Stock ETF）は `holdings.csv` に存在するが、
`universe_master` に登録されない（ETFは `is_excluded=1` で除外）。
このため EODHD の Bulk EOD フェッチ対象外となり、`price_history` にデータがない。

`_get_latest_price()` が None を返す → `evaluate_holdings()` で警告ログを出してスキップ
→ **Slack 通知に VT が一切出てこない**という状態だった。

### 10.3 採用した修正方針

| 問題 | 修正方針 | 理由 |
|------|---------|------|
| 重複行蓄積 | `sync_holdings_from_csv()` を `UPDATE + INSERT` に変更 | UNIQUE 制約追加はスキーマ変更が大きく、歴史的な決済済み行に影響する可能性があるため |
| 既存重複行 | `init_db.py` に DATA_MIGRATION（重複行 DELETE）を追加 | `init_db` 再実行で冪等にクリーンアップ可能 |
| ETF 価格欠落 | `ensure_holding_prices()` を追加しバッチ実行時に補完フェッチ | universe_master の設計を変えず、保有銘柄だけ個別取得 |

### 10.4 実装内容

#### src/holdings_monitor.py

- `sync_holdings_from_csv()`: `upsert()` を廃止し、内部で `_dedup_open_holdings()` + `_upsert_holdings()` を呼ぶ形に変更
- `_dedup_open_holdings(conn)`: 同一 ticker/is_closed=0 の重複行を削除（最大 id を残す）
- `_upsert_holdings(conn, rows)`: ticker+is_closed=0 をキーとした UPDATE/INSERT
- `ensure_holding_prices(target_date)`: price_history にデータがない保有銘柄を EODHD から補完フェッチして保存

```python
# バッチスクリプトでの呼び出し順序（evening/morning 共通）
sync_holdings_from_csv()        # CSV → DB 同期（重複なし）
ensure_holding_prices(_TODAY)   # ETF等の価格を補完
evaluate_holdings(_TODAY)       # 損益・ラベル判定
```

#### scripts/init_db.py

- `DATA_MIGRATION_STATEMENTS` リストを新設
- 既存重複行を削除する SQL を追加（冪等・初回実行時は 0 行削除）
- `_run_migrations()` で DATA_MIGRATION_STATEMENTS も実行するよう拡張

#### scripts/batch_evening_jp.py / batch_morning_all.py

- `ensure_holding_prices` をインポートし、`evaluate_holdings` 前に呼び出し追加

### 10.5 検証結果

```
# マイグレーション実行後の holdings テーブル
holdings: 3行（VT×1, NVDA×1, GOOG×1）← 18行から正常に削減

# テスト実行結果
Ran 14 tests in 0.079s: OK（全パス）
```

新規追加テスト（`tests/test_holdings_monitor.py`）:
- `DeduplicateOpenHoldingsTests`: 重複削除ロジック（2テスト）
- `UpsertHoldingsTests`: UPDATE/INSERT振り分け（3テスト）
- `SyncHoldingsFromCsvIdempotencyTests`: 3回呼んでも重複しない（1テスト）
- `EnsureHoldingPricesTests`: 補完フェッチ・スキップ（2テスト）

### 10.6 今後の TODO（保留事項）

| 項目 | 内容 |
|------|------|
| holdings UNIQUE 制約 | 長期的には `(ticker, is_closed=0)` に対する部分ユニーク制約を追加するのが望ましい（SQLite は部分インデックスの UNIQUE に非対応のため、代替案として CHECK 制約か application 側管理） |
| ETF テクニカル指標 | VT 等の ETF は price_history に補完されても technical_snapshot は計算されない。売り検討条件（MA クロス等）は機能しないため注意 |
| SNS/ニュース Deep Research 活用 | 実現のためには NewsAPI・X API・Perplexity API 等の外部サービス契約が必要。API キー未設定のためブロッカー。config.json に拡張ポイントを設けることは可能 |
| 通知内容の拡充 | ~~return_5d/return_20d/volatility_20d 等、計算済みだが通知に出ていない指標を追加可能。~~ → **セクション11で実装済み（v1.4）** |

---

## 11. 通知内容拡充・日次HTMLレポート（v1.4）

### 11.1 背景と方針

既存のSlack通知は価格・損益率・条件チェックリストのみで、「なぜその判断か」を裏付ける数値が初心者には伝わりにくかった。  
Slackはコンパクトに保ちつつ、詳細はHTMLレポートで補完する2段構え方針を採用した。

### 11.2 Slack通知の変更点

#### 買い候補ブロック（`_entry_set_block()`）

| 追加項目 | 表示例 | 取得元 |
|----------|--------|--------|
| ランク順位 | `ランク1位` | `_rs_rank`（vol_ratio降順） |
| 損切り→利確を1行化 | `損切りライン: 138.00（-8.0%）→ 利確目安: 174.00（+16.0%）  リスクリワード2:1` | 既存フィールド |
| RSI実値 | `RSI: 55（適正 50–70）` | `_rsi14`・config の pattern_[ab]_rsi_[min/max] |
| 出来高倍率 | `出来高: 2.3倍（平均比）` | `_vol_ratio` |

`_rsi14`/`_vol_ratio`/`_rs_rank` は `entry_calculator.calc_entry_set()` の返り値に `_` プレフィックス付きで追加（DB保存対象外）。  
これらは `signal_detector` の candidates が元々保持しているフィールドをパススルーするだけなので、DB変更は不要。

#### 保有銘柄ブロック（`_holding_block()`）

| 追加項目 | 表示例 | 取得元 |
|----------|--------|--------|
| 保有日数 | `保有: 15日目` | `holdings.entry_date` → `evaluate_holdings()` で算出 |
| 利確目安までの距離 | `目標まで: +13.5%（利確目安: 210.00）` | `trade_signal_log` の最新 `target_price` |

`holdings_monitor.evaluate_holdings()` の返り値に `holding_days`・`target_price` を追加した。

#### フッター（朝通知・夜通知共通）

```
📊 <http://127.0.0.1:5100/report|詳細レポートを見る>
```

order_server が未起動の場合も通知は正常送信される（リンクが開けないだけ）。

### 11.3 日次HTMLレポートページ（`/report`）

`scripts/order_server.py` は `/report` と `/health` のみを提供するレポートサーバー。Bootstrap CDN を使用したシンプルなHTML。`python -m scripts.order_server` で起動する。

**表示内容:**

| セクション | 内容 |
|------------|------|
| 市場環境 | 最新の `market_environment` レコード（モード・日経平均・S&P500 vs MA25） |
| 保有銘柄テーブル | ticker / 現在値 / 損益% / SL / 利確目安 / 保有日数 / 数量 |
| 直近7日間のシグナル履歴 | `trade_signal_log` から7日分（日付・パターン・エントリー/SL/利確/推奨投入額） |

### 11.4 テスト（`tests/test_notifier.py`）

新規追加（16テスト）:

- `EntrySetBlockTests`（10テスト）: ランク表示・RSI/出来高表示・パターン別RSI適正範囲・損切り→利確1行・マイナス符号・警告フラグ
- `HoldingBlockTests`（6テスト）: 保有日数・目標距離・理由文の表示/非表示
