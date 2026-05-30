# 投資判断支援システム

> **注意**: 本システムは投資助言や利益保証を行うものではありません。検出される候補はあくまで「検討対象」であり、注文指示ではありません。最終的な売買判断と発注操作はユーザー自身の責任で行ってください。投資には元本割れのリスクがあります。

日本株・米国株を対象とした投資判断支援システム。日次の価格データからテクニカル指標を計算し、買いパターンの検出・保有銘柄の損切り判定・Slack通知を自動で行う。最終的な売買判断はユーザー自身が行う「実行規律補助システム」。

## できること

- 日本株約3,600銘柄・米国株約51,000銘柄の日次価格データを自動取得
- 2種類の買いパターン（モメンタムブレイクアウト / 押し目買い）を自動検出
- **戦略モード切替**: 守り（インデックス並みの分散運用）と攻め（2倍・3倍株狙い）の 2 種類の戦略を `strategy_mode` で切り替え可能（詳細は [offensive_strategy.md](offensive_strategy.md)）
- 攻めモード時のみ: S/A ランク絞り込み・利確 +1R/+3R/+5R（各20%）+ 残り40%は MA25/MA50 トレーリング・買い増し候補検知・損切り引き下げ防止・MA50 撤退ルールを適用
- 検出銘柄にエントリー価格・損切りライン・利確目安・推奨投入額をセットで算出（全条件一致候補とスコア候補を区別）
- 保有銘柄の損切りライン到達を最優先で通知
- 市場全体のトレンドを判定し、警戒時は新規買いを自動停止
- 朝（07:30）と夜（20:30）の2回、Slackで通知
- Webダッシュボード（`http://127.0.0.1:5100/report`）で保有状況・シグナル履歴・チャートを確認
- 初心者向けルール確認ダッシュボード（`http://127.0.0.1:5100/rules`）で、買ってよい条件・資金管理・出口・検証・停止ルールを確認
- スコアリング定義ダッシュボード（`http://127.0.0.1:5100/scoring`）で、共通指標から算出する総合スコア・Pattern A/B/C 適合度・ランク閾値・候補化条件を確認
- 朝・夜バッチで資産総額（保有時価合計）を自動スナップショットし、ダッシュボードに表示
- ポートフォリオ全体のリスク指標（VaR/Beta/Sharpe）をダッシュボードに表示
- 保有銘柄の決算カレンダー（今後30日）を自動表示、3日以内は警告色で強調
- システムヘルスバー（最終バッチ実行状況・エラー数・データ鮮度）を常時表示
- ダークモード対応（OS設定自動検出 + 手動切替トグル）
- スクリーニングファネルで全銘柄→最終候補への絞り込み過程・各条件の充足率を可視化
- Webダッシュボード上で資金管理・シグナル・スクリーニングの各パラメータを変更可能（`http://127.0.0.1:5100/settings`）
- 月次レポートで毎月の入金額を控除したリターンと、月15%目標との差を確認
- 月間損失・連続損失が設定ラインに達した場合、新規エントリーを自動停止
- ペーパートレードはシグナル当日の終値ではなく、翌営業日の始値で仮想約定し、約定当日は利確・損切り判定を行わない
- LLMアドバイザー（任意）：検討候補・保有銘柄のデータに加え、直近の市場ニュースをChatGPT・Claude・Geminiに自動送信し、フィードバックをSlack通知に表示
- PayPayポイント運用トラッキング（任意）：コース別のポイント残高・損益率をSlack通知に表示、S&P 500/NASDAQのMA25に基づく追加・引き出しタイミングシグナルを提供。ダッシュボード（`http://127.0.0.1:5100/paypay`）から残高の確認・編集が可能

## セットアップ

詳細な手順は [investment_support/SETUP.md](investment_support/SETUP.md) を参照。

```cmd
cd investment_support
python -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt   # yfinance 含む（JP日次・初回履歴取得に使用）

copy config_template.json config.json   # APIキー等を設定
python -m scripts.init_db               # DB初期化
python -m scripts.fetch_historical      # 過去3年分の価格データ取得（数時間）
```

### 必要なAPIキー

| サービス | 用途 | config.json のキー | 費用 |
|---------|------|-------------------|------|
| EODHD | 日次価格データ（US）・インデックス | `eodhd.api_key` | $19.99/月 |
| Slack Incoming Webhook | 通知送信 | `slack.webhook_url` | 無料 |
| J-Quants（任意） | JP財務データ・決算予定 | `jquants.api_key` | 無料プランあり |
| OpenAI（任意） | LLMアドバイザー（ChatGPT） | `llm.openai.api_key` | 従量制（月額約$0.15） |
| Anthropic（任意） | LLMアドバイザー（Claude） | `llm.anthropic.api_key` | 従量制（月額約$1.50） |
| Google Gemini（任意） | LLMアドバイザー（Gemini） | `llm.gemini.api_key` | 無料枠あり（月額約$0.05） |

### 目標・停止ルールの主要設定

`config.json` の以下を、実際の運用前に確認してください。

```json
"investment_goal": {
  "monthly_deposit_jpy": 10000,
  "target_monthly_return_pct": 15.0
},
"risk_control": {
  "monthly_loss_stop_pct": 6.0,
  "max_consecutive_losses_before_pause": 3
}
```

- `monthly_deposit_jpy`: 毎月追加する入金額。月次レポートではこの入金分を除いてリターンを計算する
- `target_monthly_return_pct`: 検証目標。利益保証ではなく、実績との差を確認するための基準
- `monthly_loss_stop_pct`: 月間の確定損失がこの割合に達したら新規買いを停止する
- `max_consecutive_losses_before_pause`: 損失トレードが連続したときの停止ライン

### 戦略モードの切替（守り/攻め）

`config.json` の `strategy_mode` で 2 種類の戦略を切り替えられる。詳細は [offensive_strategy.md](offensive_strategy.md) を参照。

| モード | 既定値 | 内容 |
|--------|--------|------|
| `defensive` | はい | 現行の守り戦略。リスク 2% 固定、利確タイプI=+0.5R/50%、保有上限 3 銘柄 |
| `offensive` | いいえ | 攻め口座向け 2倍・3倍株狙い戦略。S/Aランクのみ候補化、+1R/+3R/+5R 各20%+残り40%トレーリング、買い増し検知、MA25/MA50段階撤退、保有 5〜8 銘柄、1 銘柄最大 20% |

```json
"strategy_mode": "offensive",
"strategy": { "offensive": { ... } }
```

切り替えは `/settings` 画面または `config.json` の編集で行う。`strategy_mode = "defensive"` のときは挙動が完全に現行互換となるよう実装されている。

## 日常の使い方

すべてのコマンドは `investment_support/` ディレクトリから実行する。

### 自動実行（Windowsタスクスケジューラ登録済み）

| 時刻 | バッチ | 内容 |
|------|-------|------|
| ログオン時 | `python -m scripts.order_server` | Webダッシュボード自動起動（`127.0.0.1:5100`） |
| 毎日 20:30 | `python -m scripts.batch_evening_jp` | JP株価取得 → テクニカル計算 → シグナル検出 → Slack夜通知 |
| 毎日 07:30 | `python -m scripts.batch_morning_all` | US株価取得 → テクニカル計算 → JP+USシグナル統合 → Slack朝通知 |
| 毎月1日 08:00 | `python -m scripts.batch_monthly` | 銘柄マスタ更新 → 月次レポート送信 |

タスクスケジューラへの一括登録：
```powershell
# 管理者権限のPowerShellで実行
.\setup_scheduler.ps1
```

### 手動実行

```cmd
python -m scripts.batch_evening_jp      # 夜バッチを今すぐ実行
python -m scripts.batch_morning_all     # 朝バッチを今すぐ実行
python -m scripts.batch_monthly         # 銘柄マスタ更新 + 月次レポート
python -m scripts.order_server          # Webダッシュボード起動（127.0.0.1:5100）
python -m scripts.add_trade_result      # トレード結果の手動記録
python -m scripts.fetch_historical      # 履歴データの再取得（冪等）
python -m scripts.backtest_signals --start 2024-01-01 --end 2026-04-30  # バックテスト
python -m scripts.update_paper_trades --summary  # ペーパートレードのサマリー表示
```

### 保有銘柄の管理

`investment_support/holdings.csv` を直接編集する。moomoo OpenD が起動中の場合はバッチ実行時に保有銘柄が自動同期される（任意・未接続でも動作する）。

```csv
ticker,name,market,entry_price,entry_date,quantity,stop_loss_price,memo
7203,トヨタ自動車,JP,3200,2025-01-15,10,,
AAPL,Apple Inc,US,185.5,2025-01-10,5,170.0,
```

- `stop_loss_price` を省略すると `entry_price × 0.92`（-8%）が自動設定される
- `quantity` を省略すると 1 として計算される

### PayPayポイント運用のトラッキング

`config.json` で `"paypay_points": {"enabled": true}` を設定し、`investment_support/paypay_points.csv` にPayPayアプリで確認したポイント残高を入力する。

```csv
course,invested_points,current_points,last_updated
standard,5000,5230,2026-05-16
technology,3000,2850,2026-05-16
gold,1000,1015,2026-05-16
tech_challenge,2000,2340,2026-05-16
```

- `course`: コースキー（standard / technology / challenge / tech_challenge / gold 等）
- `invested_points`: 累計投入ポイント（追加合計 - 引き出し合計）
- `current_points`: PayPayアプリに表示されている現在のポイント残高
- `last_updated`: アプリで確認した日（3日以上前だと通知に⏳マークが付く）
- バッチ実行時にポートフォリオ総資産に自動加算され、Slack通知にコース別損益とタイミングシグナルが表示される
- PayPayポイント運用にはAPIがないため、残高の更新は手動で行う

### トレード結果の記録

Performanceページ（`http://127.0.0.1:5100/performance`）にデータを表示するには、トレード結果の記録が必要。以下の2つの方法がある。

**方法1: moomoo自動同期（推奨）**

moomoo OpenD が起動中の場合、バッチ実行時に約定履歴（過去90日）が自動取得され `trade_result_log` に記録される。BUY/SELL を自動ペアリングし、P&L を計算する。対応するシグナルがない場合は手動エントリーとして自動作成される。

**方法2: ダッシュボードから手動記録**

Performanceページの「+ トレード結果を記録」ボタンからフォーム入力で記録できる。ティッカーを入力すると対応するシグナルが自動検索される。シグナルがない銘柄も記録可能。

- moomoo OpenD 未接続時は自動同期がスキップされる（ダッシュボードからの手動記録は常に利用可能）
- moomoo の約定履歴は過去90日分のみ取得可能

## アーキテクチャ

### データフロー

```
外部API（EODHD / yfinance / J-Quants）
  ↓
price_history / index_snapshot（SQLite）
  ↓
technical_snapshot（MA, RSI, ATR 等を計算）
  ↓
market_environment（通常 / 警戒 / 全面警戒を判定）
  ↓
daily_signals → trade_signal_log（パターンA/B検出 → エントリーセット算出）
  ↓
Slack通知 + Webダッシュボード（/report）
```

### 売買パターン

| パターン | 名称 | 検出条件の要約 |
|---------|------|--------------|
| A | モメンタムブレイクアウト | MA200上昇中 + MA整列（MA5>MA25>MA75>MA200） + 52週高値更新 + 出来高急増 + RSI 50-70 |
| B | 押し目買い | MA200上昇中 + MA整列 + 直近5日以内にMA25タッチ後反発 + MA25上昇中 + RSI 40-60 |
| C | サポートバウンス | 週足MA25上昇中 + サポートレベル近傍 + RSI ≤35（売られ過ぎ） + パラボリックSAR上昇転換 |

`candidate`（検討候補）は、全条件を満たした「全条件一致候補」と、共通指標ベースの総合スコア50点以上かつ主要パターン適合度60点以上で昇格した「スコア候補」に分かれる。Pattern A/B/C は総合スコアの直接材料ではなく、同じ共通指標から算出する勝ち筋ラベルとして扱う。スコア候補は未達条件が残るため、通知の条件チェック欄を確認してから判断する。攻めモードでは、さらに `strategy.offensive.min_final_score` / `min_rank` の絞り込みを通過した銘柄だけが新規候補として残る。

### 市場環境モード

| モード | 条件 | 新規買いシグナル |
|-------|------|----------------|
| 通常（normal） | 日経平均 ≥ MA25 かつ S&P500 ≥ MA25 | 発信する |
| 警戒（caution） | いずれか一方が MA25 未満 | 停止 |
| 全面警戒（full_caution） | 両方が MA25 未満 | 停止 |

保有銘柄の損切り判定は、市場環境に関わらず常に実行される。

### エントリーセット計算

| 項目 | 計算方法 |
|------|---------|
| エントリー価格 | 当日終値（翌日寄付想定） |
| 損切りライン | エントリー価格 - ATR14 × 2 |
| 利確目安 | リスクリワード 2:1（損切り幅の2倍） |
| 推奨投入額 | `総資金 × 2% ÷ 損切り幅(%)` を基本に、`総資金 ÷ 有効保有枠` を上限にする |

既存保有数が `max_concurrent_holdings` に達している場合は、通知上で保有枠満杯の警告を表示する。

### ディレクトリ構成

```
investment_support/
├── config.json               # 設定ファイル（APIキー等）※Git管理外
├── config_template.json      # 設定テンプレート
├── holdings.csv              # 保有銘柄（手動 or moomoo同期）
├── paypay_points.csv         # PayPayポイント運用残高（手動入力）
├── requirements.txt
├── SETUP.md
│
├── src/
│   ├── config.py             # CONFIG グローバル変数
│   ├── logger.py             # ロギング（コンソール + logs/batch_YYYYMMDD.log）
│   ├── data_fetcher.py       # EODHD API・JPX銘柄マスタ取得
│   ├── yfinance_fetcher.py   # yfinance による価格取得（JP日次 + 初回一括）
│   ├── jquants_fetcher.py    # J-Quants API（JP財務・決算予定）
│   ├── data_store.py         # SQLite操作・バックアップ
│   ├── technical_calculator.py  # テクニカル指標計算
│   ├── market_filter.py      # 市場環境判定・流動性フィルタ
│   ├── signal_detector.py    # パターンA/B検出
│   ├── scoring.py            # 銘柄スコアリング（0-100点 → S/A/B/C/Watchランク）
│   ├── entry_calculator.py   # エントリーセット算出
│   ├── holdings_monitor.py   # 保有銘柄の損益計算・損切り判定
│   ├── moomoo_sync.py        # moomoo OpenD からの保有銘柄同期
│   ├── risk.py               # リスク指標（VaR, Beta, Sharpe Ratio）
│   ├── risk_control.py       # 月間損失・連続損失による新規買い停止ルール
│   ├── portfolio_snapshot.py # バッチ実行時の総資産スナップショット
│   ├── backtester.py         # バックテスト（過去シグナルの期待値検証）
│   ├── paper_trading.py      # ペーパートレード（仮想売買記録・日次更新）
│   ├── paypay_points.py      # PayPayポイント運用トラッキング
│   ├── notifier.py           # Slack通知生成・送信
│   ├── llm_advisor.py        # LLMアドバイザー（ChatGPT/Claude/Gemini自動フィードバック）
│   ├── news_fetcher.py       # ニュース取得（Google News RSS）
│   └── report_generator.py   # 月次レポート生成（運用統計付き）
│
├── scripts/
│   ├── init_db.py            # DB初期化（冪等）
│   ├── fetch_historical.py   # 初回3年分一括取得
│   ├── batch_evening_jp.py   # 夜バッチ（20:30）
│   ├── batch_morning_all.py  # 朝バッチ（07:30）
│   ├── batch_monthly.py      # 月次バッチ
│   ├── order_server.py       # Webダッシュボード（Flask, 127.0.0.1:5100）
│   ├── backtest_signals.py   # バックテスト実行CLI
│   ├── update_paper_trades.py # ペーパートレード更新・サマリー表示
│   ├── add_trade_result.py   # トレード結果の手動記録
│   └── sync_holdings_from_moomoo.py  # moomoo手動同期
│
├── tests/                    # unittest
├── data/                     # SQLite DB + バックアップ
└── logs/                     # 日次ログ
```

### データベース（SQLite）

主要テーブル：

| テーブル | 内容 |
|---------|------|
| `universe_master` | 銘柄マスタ（JP/US、約55,000銘柄） |
| `price_history` | 日足OHLCV |
| `technical_snapshot` | 日次テクニカル指標（MA, RSI, ATR等） |
| `index_snapshot` | インデックス日次データ（N225, TOPIX, SP500, NASDAQ） |
| `market_environment` | 日次の市場環境モード |
| `holdings` | 保有銘柄（holdings.csv から同期） |
| `daily_signals` | 日次判定結果 |
| `trade_signal_log` | 通知済みエントリーセット |
| `trade_result_log` | トレード実績（手動記録） |
| `paper_trades` | ペーパートレードの仮想約定・決済履歴 |
| `fundamentals_snapshot` | JP財務データ（J-Quants） |
| `earnings_schedule` | JP決算発表予定日 |
| `take_profit_targets` | 値動きタイプ別の分割利確・トレーリング管理 |
| `portfolio_snapshot` | バッチ実行時の総資産スナップショット |
| `llm_feedback_log` | LLMアドバイザーの応答ログ |
| `paypay_points` | PayPayポイント運用コース別残高（paypay_points.csv から同期） |

`init_db.py` は冪等（`IF NOT EXISTS` + マイグレーション）。スキーマの詳細は [detailed_design.md](detailed_design.md) セクション2を参照。

## テスト

```cmd
cd investment_support
python -m unittest discover -s tests        # 全テスト実行
python -m unittest tests.test_notifier      # 個別テスト
```

## 関連ドキュメント

| ファイル | 内容 |
|---------|------|
| [SETUP.md](investment_support/SETUP.md) | セットアップ手順（スクリーンショット付き） |
| [detailed_design.md](detailed_design.md) | 詳細設計書（DB定義・バッチフロー・Slack通知テンプレート） |
| [low_cost_investment_support_requirements.md](low_cost_investment_support_requirements.md) | 要件定義書 |
| [CLAUDE.md](CLAUDE.md) | Claude Code 向けガイド |

## 運用開始前チェックリスト

実際に資金を投じる前に、以下をすべて確認してください。

- [ ] `python -m unittest discover -s tests` が全件パスする
- [ ] `config.json` の `total_capital_jpy` / `total_capital_usd` が実際の運用資金と一致している
- [ ] `config.json` の `investment_goal` / `risk_control` を確認し、`/rules` 画面で投資ルールを理解している
- [ ] `holdings.csv` に重複行がない（同一銘柄が2行以上ないこと）
- [ ] Slack通知のテスト送信ができる（`python -m scripts.batch_evening_jp` を手動実行して通知を確認）
- [ ] 警戒モード時に新規検討候補が通知されないことを確認済み
- [ ] `data/backup/` にDBバックアップが作成されている
- [ ] `config.json` に `"paper_mode": {"enabled": true}` を設定し、最低1か月はペーパートレードで検証する
- [ ] ペーパートレード結果（`python -m scripts.update_paper_trades --summary`）で期待値がプラスであることを確認
- [ ] `python -m scripts.backtest_signals --start 2024-01-01 --end YYYY-MM-DD` で過去シグナルの期待値を確認

## 既知の制約

- JP銘柄の価格データは yfinance 経由で取得しており、EODHD Bulk EOD は JP 未対応（代替候補は [investment_support/docs/jp_price_sources.md](investment_support/docs/jp_price_sources.md) を参照）
- JPX銘柄一覧Excelのエンコーディング問題により、JP銘柄名・セクター名が文字化けする場合がある（銘柄コードでの照合には影響なし）
- J-Quants APIキー未設定の場合、JP財務データ・決算アラートは機能しない
- Screening List の日付は `daily_signals` の最新日。流動性フィルタが 0 件だと更新されない（`allow_unknown_market_cap` が OFF かつ時価総額未登録のとき等）。Slack に警告が出る
- US の発行済株式数は月次バッチで EODHD から段階的に補完（`screening.us_shares_refresh_max_per_monthly`）。JP は J-Quants 月次更新
- Webダッシュボードは `127.0.0.1` のみで待ち受け。外部公開は不可
- 初回セットアップ時に `fetch_historical` で3年分の履歴取得が必要（数時間かかる）
