# 投資判断支援システム 詳細設計書 v1.2

## 1. 文書概要

本書は、要件定義書 v2.0 をもとに、DB定義・バッチフロー設計・Slack通知テンプレート・ディレクトリ構成・設定ファイル仕様を定義する。

### 1.1 変更履歴

#### v1.2（2026-04-11）

- 半自動注文機能を追加（セクション11）
  - Slack 通知にワンクリック注文リンクを埋め込み
  - ローカル Web サーバーで確認画面を表示→成行注文を発注
  - `src/order_executor.py`, `scripts/order_server.py` を新規追加
  - config.json に `order_server` セクション・`moomoo.trade_password_md5` を追加

#### v1.1（2026-04-11）

- 初回セットアップフロー（yfinance による3年分履歴一括取得）を追記
- moomoo OpenAPI 連携（保有ポジション自動同期）セクションを追加
- ディレクトリ構成を実装に合わせて更新（実行用 .bat / .ps1 / SETUP.md 等）
- 設定ファイルに `moomoo` セクションを追加
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
平日（日本時間）
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
├── holdings.csv              # 保有銘柄管理ファイル（手動更新 or moomoo同期）
├── requirements.txt          # Python依存パッケージ一覧
├── SETUP.md                  # セットアップ手順書
│
├── run_evening_jp.bat        # 夜バッチ起動用バッチファイル
├── run_morning_all.bat       # 朝バッチ起動用バッチファイル
├── run_monthly.bat           # 月次バッチ起動用バッチファイル
├── run_order_server.bat      # 注文確認Webサーバー起動用
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
│   ├── notifier.py           # Slack Block Kit 生成・送信・注文リンク埋め込み
│   ├── order_executor.py     # moomoo 経由の成行注文発注・署名生成
│   └── report_generator.py   # 月次集計レポート生成
│
├── scripts/
│   ├── init_db.py            # DB初期化（DDL実行）
│   ├── fetch_historical.py   # 初回3年分履歴データ一括取得（yfinance使用）
│   ├── sync_from_moomoo.py   # moomoo保有ポジション → holdings.csv 同期
│   ├── order_server.py       # 注文確認用ローカルWebサーバー（Flask）
│   ├── batch_evening_jp.py   # 夜バッチ エントリーポイント
│   ├── batch_morning_all.py  # 朝バッチ エントリーポイント
│   ├── batch_monthly.py      # 月次バッチ エントリーポイント
│   └── add_trade_result.py   # トレード結果の手動入力ツール
│
└── tests/
    ├── __init__.py
    └── test_sync_from_moomoo.py  # moomoo同期のユニットテスト
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
    "max_candidates_per_pattern": 3,
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

  "moomoo": {
    "host": "127.0.0.1",
    "port": 11111,
    "security_firm": "FUTUJP",
    "trade_password_md5": ""
  },

  "order_server": {
    "host": "127.0.0.1",
    "port": 5100,
    "secret": "YOUR_RANDOM_SECRET_HEX"
  }
}
```

### 6.1 moomoo セクション仕様

| キー | 型 | 必須 | 説明 |
|------|-----|------|------|
| host | TEXT | ✅ | OpenD の接続先ホスト（通常 127.0.0.1） |
| port | INTEGER | ✅ | OpenD の接続先ポート（デフォルト 11111） |
| security_firm | TEXT | ✅ | 証券会社コード。moomoo Japan の場合は `FUTUJP` |
| trade_password_md5 | TEXT | | 取引パスワードの MD5 ハッシュ。半自動注文機能使用時に必要 |

※ `security_firm` を正しく設定しないと、moomoo Japan のリアル口座が見えない。

### 6.2 order_server セクション仕様

| キー | 型 | 必須 | 説明 |
|------|-----|------|------|
| host | TEXT | ✅ | Web サーバーのリスンホスト（必ず 127.0.0.1） |
| port | INTEGER | ✅ | Web サーバーのポート（デフォルト 5100） |
| secret | TEXT | ✅ | HMAC 署名用秘密鍵。ランダムな16バイト以上の文字列 |

※ `secret` が空の場合、Slack 通知に注文リンクは埋め込まれない（通常の通知のみ）。

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

[4] moomoo 同期（任意）
    ├─ 前提: moomoo アプリ + OpenD が起動済み
    └─ python -m scripts.sync_from_moomoo --dry-run
        → 確認後、--dry-run を外して実行

[5] タスクスケジューラ登録
    └─ PowerShell (管理者) で setup_scheduler.ps1 を実行
        → 夜バッチ(20:30)・朝バッチ(07:30)・月次バッチ(毎月1日 08:00) を登録
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

## 9. moomoo OpenAPI 連携

### 9.1 概要

moomoo（富途証券）の OpenD ローカルデーモンを介して、保有ポジションを自動取得し holdings.csv に同期する。

### 9.2 前提条件

- moomoo アプリがインストール済みであること
- OpenD（ローカルデーモン）が起動していること（デフォルト: 127.0.0.1:11111）
- futu-api パッケージがインストール済みであること
- config.json の `moomoo.security_firm` が正しく設定されていること（moomoo Japan: `FUTUJP`）

### 9.3 処理フロー

```
python -m scripts.sync_from_moomoo [--dry-run] [--env auto|real|simulate]
│
├─ [1] OpenD に接続（config.json の host / port）
├─ [2] security_firm を解決（config → 自動検出フォールバック）
├─ [3] 取引口座を自動検出
│   ├─ --env auto（デフォルト）: REAL 口座を優先 → 無ければ SIMULATE
│   ├─ --env real: REAL 口座のみ使用
│   └─ --env simulate: SIMULATE 口座のみ使用
├─ [4] 対応市場（US, JP）ごとにポジション取得
│   └─ HK, SH, SZ はシステム未対応のためスキップ
├─ [5] holdings.csv と照合
│   ├─ 新規: 行を追加（stop_loss_price = entry_price × 0.92）
│   ├─ 既存: entry_price, quantity のみ更新（stop_loss_price は保持）
│   └─ moomoo にない行: 削除しない（手動管理分を保護）
└─ [6] holdings.csv に書き込み
```

### 9.4 対応市場マッピング

| moomoo 市場コード | 内部市場コード | 対応状況 |
|-------------------|--------------|----------|
| US | US | ✅ 対応 |
| JP | JP | ✅ 対応 |
| HK | - | ❌ 未対応（スキップ） |
| SH | - | ❌ 未対応（スキップ） |
| SZ | - | ❌ 未対応（スキップ） |

---

## 10. 半自動注文機能

### 10.1 概要

Slack 通知に「ワンクリック注文」リンクを埋め込み、クリック → ブラウザで確認 → 成行注文を発注する半自動注文機能。

### 10.2 アーキテクチャ

```
[Slack 通知]
  │ 📱 ワンクリック買い注文（2株）  ← HMAC 署名付き URL
  │
  ▼ ユーザーがリンクをクリック
[ブラウザ: 確認画面]  ← localhost:5100 (Flask)
  │ 銘柄・数量・口座タイプ・取引環境を表示
  │ 数量は変更可能
  │
  ▼ 「注文を実行する」ボタン
[order_executor.py]
  │ moomoo OpenD に接続
  │ unlock_trade → place_order (成行注文)
  │
  ▼
[ブラウザ: 結果画面]
  └─ 注文ID表示 or エラーメッセージ
```

### 10.3 セキュリティ設計

| 対策 | 内容 |
|------|------|
| localhost 限定 | Web サーバーは 127.0.0.1 でのみリッスン（外部アクセス不可） |
| HMAC 署名 | 注文リンクの URL にパラメータ署名を付与。改ざん検知 |
| ブラウザ確認 | リンク押下だけでは発注されない。確認画面での操作が必要 |
| 取引パスワード | moomoo の unlock_trade に MD5 ハッシュが必要（config.json に保存） |

### 10.4 対応する注文

| 注文 | トリガー | Slack 通知内の表示位置 |
|------|---------|---------------------|
| 買い（成行） | 買い候補（candidate）が検出された時 | 夜通知・朝通知の各候補の末尾 |
| 売り（成行） | 強制損切り / 売り検討の保有銘柄 | 夜通知・朝通知の保有銘柄ブロック末尾 |

### 10.5 数量の自動計算

- **買い**: `推奨投入額 ÷ エントリー価格`
  - US 株: 小数第2位まで（端株対応）
  - JP 株: 100株単位に切り捨て
- **売り**: holdings.csv の `quantity` をそのまま使用
- 確認画面で数量は変更可能

### 10.6 前提条件

1. `run_order_server.bat`（または `python -m scripts.order_server`）でサーバーが起動していること
2. OpenD が起動していること
3. config.json に以下が設定されていること:
   - `order_server.secret`（HMAC 署名用秘密鍵）
   - `moomoo.trade_password_md5`（取引パスワードの MD5。REAL 環境のみ必要）

### 10.7 注文サーバーの起動

```
python -m scripts.order_server
```

または `run_order_server.bat` をダブルクリック。タスクスケジューラに登録してPC起動時に自動起動することも可能。

---

## 11. 未解決事項

| 項目 | 内容 | 状態 |
|------|------|------|
| EODHD の発行済株式数の取得方法 | fundamental API が必要な可能性あり | 🔴 未解決。取得不可の場合は時価総額フィルタを一時的に無効化 |
| 日本株のティッカー形式 | EODHD では「7203.T」形式 | ✅ 解決済み。fetch_bulk_eod 内で `.T` / `.US` サフィックスを自動除去 |
| 米国株の市場休場日リスト | EODHD で休場日フラグが取れるか未確認 | 🔴 未解決。取れない場合は NYSE 休場日カレンダーをローカルで管理 |
| Windows タスクスケジューラの設定 | 具体的な登録手順が未定 | ✅ 解決済み。`setup_scheduler.ps1` で一括登録（管理者権限で実行） |
