# セットアップ手順

## 1. プロジェクトのセットアップ

コマンドプロンプトで `freelance_scout/` フォルダに移動して以下を実行。

```cmd
cd C:\Users\bun22\Desktop\Ishikawa238\FreelanceScout\freelance_scout

# 仮想環境を作成
python -m venv .venv

# 仮想環境を有効化（cmd の場合）
.venv\Scripts\activate.bat

# 依存パッケージをインストール（shared モジュール含む）
pip install -e "../../[slack]" -r requirements.txt
```

---

## 2. 設定ファイルの作成

```cmd
copy config_template.json config.json
```

`config.json` をテキストエディタで開き、以下を設定する。

| 項目 | 設定箇所 | 内容 |
|------|---------|------|
| Slack Webhook URL | `slack.webhook_url` | Slack の Incoming Webhook URL |
| CW キーワード | `crowdworks.keywords` | 検索キーワード（任意に変更可） |
| CN カテゴリ | `coconala.category_ids` | 対象カテゴリID |
| 除外キーワード | `filter.exclude_keywords` | 除外したいキーワード |

Webhook URL が未設定（`XXXXX` のまま）の場合、通知はコンソールに出力される。

---

## 3. DB の初期化

```cmd
python -m scripts.init_db
```

`data/freelance_scout.db` が作成される。

---

## 4. 動作確認

```cmd
python -m scripts.batch_morning
```

正常に動作すると以下のような出力になる：
```
[FreelanceScout 朝バッチ] 開始: 2026-04-25
STEP 1: CrowdWorks スクレイプ
CrowdWorks: 500 件取得
STEP 2: Coconala スクレイプ
Coconala: 200 件取得
STEP 3: フィルタ
新着案件: 380 件
STEP 4: DB 保存
STEP 5: Slack 通知
[FreelanceScout 朝バッチ] 完了: success
```

2回目以降は重複排除により新着が減る（同日2回目は 0 件になる）。

---

## 5. Windows タスクスケジューラへの登録

PowerShell を **管理者として実行** し、以下を実行する。

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
cd C:\Users\bun22\Desktop\Ishikawa238\FreelanceScout\freelance_scout
.\setup_scheduler.ps1
```

| タスク名 | 実行時刻 | 内容 |
|---------|---------|------|
| FreelanceScout_MorningBatch | 毎日 07:00 | スクレイプ → フィルタ → Slack 通知 |

登録確認: `taskschd.msc` を開いてタスクが表示されれば OK。
