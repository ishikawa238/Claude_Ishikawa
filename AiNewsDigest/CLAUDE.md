# CLAUDE.md

## Project Overview

AiNewsDigest — AI関連ニュース自動収集・通知システム。複数の RSS フィードから AI/ML/LLM 関連ニュースを毎朝収集し、重複排除の後、Slack に新着を通知する。

## Setup

```bash
cd ai_news_digest
python -m venv .venv
.venv\Scripts\activate.bat
pip install -e "../../[slack]" -r requirements.txt

copy config_template.json config.json   # Webhook URL と RSS フィードを設定
python -m scripts.init_db
```

## Running Commands

すべてのコマンドは `ai_news_digest/` からモジュール実行する。

```bash
# 朝バッチ（タスクスケジューラで毎日 07:00 に登録）
python -m scripts.batch_morning

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
RSS Feeds → rss_fetcher → all_articles
  → news_filter（日付フィルタ + DB 重複排除）→ new_articles
  → DB upsert（articles テーブル）
  → notifier → Slack
```

### Key Source Modules (`src/`)

| Module | Role |
|--------|------|
| `config.py` | `config.json` を読み込み、`CONFIG` として提供 |
| `logger.py` | `shared.logger` の再エクスポート |
| `data_store.py` | SQLite 接続 + エラーログ + CRUD ヘルパー再エクスポート |
| `rss_fetcher.py` | feedparser で RSS フィード取得 |
| `news_filter.py` | 日付フィルタ + URL ベース重複排除 |
| `notifier.py` | Slack Block Kit 通知（ニュースダイジェスト） |

### Database (`data/ai_news_digest.db`)

SQLite with WAL mode. Tables:
- `articles` — 記事データ（UNIQUE: url）
- `batch_run_log` — バッチ実行ログ
- `error_log` — エラーログ

### External Dependencies

- **Slack** (`config.json > slack.webhook_url`) — Incoming Webhook
- **shared/** — 共通モジュール（config, db, slack, logger）
