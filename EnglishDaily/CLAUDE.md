# CLAUDE.md

## Project Overview

EnglishDaily — 英語学習の継続支援システム。曜日ごとに設定されたカリキュラム（単語・リスニング・シャドウイング・ライティング）を毎朝 Slack に To-Do 形式で配信し、Slack リアクションで完了を個別追跡する。未完了タスクには 17:30〜23:30 に段階的リマインドを送信。連続学習日数（ストリーク）も表示する。

## Setup

```bash
cd english_daily
python -m venv .venv
.venv\Scripts\activate.bat
pip install -e "../../[slack]"

copy config_template.json config.json   # Bot Token とチャンネルを設定
python -m scripts.init_db
```

## Running Commands

すべてのコマンドは `english_daily/` からモジュール実行する。

```bash
# 朝バッチ（タスクスケジューラで毎日 07:00 に登録）
python -m scripts.batch_morning

# リマインド（17:30 / 19:30 / 21:30 / 23:30 に登録）
python -m scripts.batch_reminder

# DB 初期化（冪等）
python -m scripts.init_db
```

## Running Tests

```bash
python -m unittest discover -s tests
```

## Architecture

### Data Flow

```
config.json (curriculum) → curriculum.py → 曜日別タスクリスト
  → notifier.py → Slack 配信 (Bot Token API)
  → daily_message テーブルに message_ts 保存

[リマインド時]
  daily_message.message_ts → Slack reactions.get
  → 未完了タスク抽出 → notifier.py → リマインド送信

[翌朝]
  前日の daily_message → reactions.get → 全完了判定
  → streak テーブル更新 → 朝の配信にストリーク表示
```

### Key Source Modules (`src/`)

| Module | Role |
|--------|------|
| `config.py` | `config.json` を読み込み、`CONFIG` として提供 |
| `logger.py` | `shared.logger` の再エクスポート |
| `data_store.py` | SQLite 接続 + エラーログ + CRUD ヘルパー再エクスポート |
| `slack_client.py` | Bot Token ベースの Slack Web API クライアント |
| `curriculum.py` | 曜日に応じたタスク一覧を config から取得 |
| `notifier.py` | 朝の配信・リマインドの Block Kit 組み立て + 送�� |
| `streak.py` | 連続学習日数の管理（前日リアクション確認 → 更新） |

### Database (`data/english_daily.db`)

SQLite with WAL mode. Tables:
- `daily_message` — 朝の配信メッセージ情報（UNIQUE: date）
- `streak` — 連続学習日数（単一行 id=1）
- `batch_run_log` — バッチ実行ログ
- `error_log` — エラーログ

### External Dependencies

- **Slack** (`config.json > slack.bot_token`) — Bot Token（`chat:write`, `reactions:read`）
- **shared/** — 共通モジュール（config, db, slack helpers, logger）

### Schedule

| Time | Task | Script |
|------|------|--------|
| 07:00 | 朝の配信 + ストリーク更新 | `batch_morning` |
| 17:30 | リマインド（やさしめ） | `batch_reminder` |
| 19:30 | リマインド（普通） | `batch_reminder` |
| 21:30 | リマインド（強め） | `batch_reminder` |
| 23:30 | リマインド（ラストコール） | `batch_reminder` |
