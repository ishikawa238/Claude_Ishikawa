# CLAUDE.md

## Project Overview

FreelanceScout — フリーランス案件自動収集・通知システム。CrowdWorks と Coconala から IT/開発系・データ/AI系の案件を毎朝スクレイプし、キーワードフィルタ・重複排除の後、Slack に新着を通知する。

## Setup

```bash
cd freelance_scout
python -m venv .venv
.venv\Scripts\activate.bat
pip install -e "../../[slack]" -r requirements.txt

copy config_template.json config.json   # Webhook URL を設定
python -m scripts.init_db
```

## Running Commands

すべてのコマンドは `freelance_scout/` からモジュール実行する。

```bash
# 朝バッチ（タスクスケジューラで毎日 07:00 に登録）
python -m scripts.batch_morning

# DB 初期化（冪等）
python -m scripts.init_db
```

## Running Tests

```bash
python -m unittest discover -s tests
python -m unittest tests.test_job_filter
python -m unittest tests.test_notifier
```

## Architecture

### Data Flow

```
CrowdWorks / Coconala → scraper → all_jobs
  → job_filter（重複排除 + キーワードマッチ）→ new_jobs
  → DB upsert（jobs テーブル）
  → notifier → Slack
```

### Key Source Modules (`src/`)

| Module | Role |
|--------|------|
| `config.py` | `config.json` を読み込み、`CONFIG` として提供 |
| `logger.py` | `shared.logger` の再エクスポート |
| `data_store.py` | SQLite 接続 + エラーログ + CRUD ヘルパー再エクスポート |
| `scraper_crowdworks.py` | CrowdWorks スクレイパー（JSON 埋め込みデータ方式） |
| `scraper_coconala.py` | Coconala スクレイパー（HTML パース方式） |
| `job_filter.py` | 重複排除 + キーワードマッチ + 除外フィルタ |
| `notifier.py` | Slack Block Kit 通知（新着案件レポート） |

### Database (`data/freelance_scout.db`)

SQLite with WAL mode. Tables:
- `jobs` — 案件データ（UNIQUE: source + job_id）
- `batch_run_log` — バッチ実行ログ
- `error_log` — エラーログ

### External Dependencies

- **Slack** (`config.json > slack.webhook_url`) — Incoming Webhook
- **shared/** — 共通モジュール（config, db, slack, logger）
