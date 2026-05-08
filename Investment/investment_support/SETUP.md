# セットアップ手順

## 1. Python のインストール

### 手順
1. https://www.python.org/downloads/ を開く
2. 「Download Python 3.11.x」（または最新の 3.11 以上）をクリック
3. インストーラを実行する
4. **「Add python.exe to PATH」に必ずチェックを入れてからインストール**

### インストール確認
コマンドプロンプト（cmd）またはPowerShellを開いて実行：
```
python --version
```
`Python 3.11.x` と表示されれば OK。

---

## 2. プロジェクトのセットアップ

コマンドプロンプトで `investment_support/` フォルダに移動して以下を実行。

```cmd
cd C:\Users\bun22\Desktop\Ishikawa238\Investment\investment_support

# 仮想環境を作成
python -m venv .venv

# 仮想環境を有効化（cmd の場合）
.venv\Scripts\activate.bat

# 依存パッケージをインストール
pip install -r requirements.txt
```

---

## 3. 設定ファイルの作成

```cmd
copy config_template.json config.json
```

`config.json` をテキストエディタで開き、以下を入力する。

| 項目 | 設定箇所 | 内容 |
|------|---------|------|
| EODHD APIキー | `eodhd.api_key` | EODHDで取得したAPIキー |
| Slack Webhook URL | `slack.webhook_url` | SlackのIncoming Webhook URL |
| 総資金（円） | `capital.total_capital_jpy` | 現在の投資可能総資金（例: 200000） |
| 総資金（ドル） | `capital.total_capital_usd` | 米国株用資金（例: 1500） |

---

## 4. DB の初期化

```cmd
python -m scripts.init_db
```

以下のような出力が出れば成功：
```
==================================================
  DB初期化完了サマリー
==================================================
  DBファイル: C:\...\data\investment.db
  テーブル数: 12
--------------------------------------------------
  batch_run_log                          0 行
  daily_signals                          0 行
  error_log                              0 行
  ...
==================================================
```

---

## 5. 保有銘柄ファイルの作成

`investment_support/` 直下に `holdings.csv` を作成する。
保有銘柄がない場合はヘッダー行だけ作成する。

```
ticker,name,market,entry_price,entry_date,quantity,stop_loss_price,memo
```

保有銘柄がある場合の記載例：
```
ticker,name,market,entry_price,entry_date,quantity,stop_loss_price,memo
7203,トヨタ自動車,JP,3200,2025-01-15,10,,
AAPL,Apple Inc,US,185.5,2025-01-10,5,170.0,
```

---

## 6. 動作確認

DB初期化後、以下のコマンドでテーブル一覧を確認できる。

```cmd
python -c "import sqlite3; conn=sqlite3.connect('./data/investment.db'); [print(r[0]) for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table' ORDER BY name\")]; conn.close()"
```

---

## 7. 運用開始前チェック

タスクスケジューラに登録する前に、[README の運用開始前チェックリスト](../README.md#運用開始前チェックリスト) をすべて確認してください。特に `paper_mode` を有効にして最低1か月の検証運用を推奨します。

---

## 8. Windowsタスクスケジューラへの登録（後工程）

バッチが完成した後に以下を登録する。

| タスク名 | 実行時刻 | 実行コマンド |
|---------|---------|-------------|
| investment_evening_jp | 毎日 20:30 | `python -m scripts.batch_evening_jp` |
| investment_morning_all | 毎日 07:30 | `python -m scripts.batch_morning_all` |
