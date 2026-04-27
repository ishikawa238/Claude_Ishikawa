# CLAUDE.md

## Project Overview

PaperWatch — 新着論文監視・通知システム。arXiv の RSS フィードから指定カテ��リの新着論文を毎朝取得し、キーワードマッチでスコアリングした上で、閾値以上の論文を Slack に通知する。主な対象領域は金融 AML（Anti-Money Laundering）× ブロックチェーン。

## Setup

```bash
cd paper_watch
python -m venv .venv
.venv\Scripts\activate.bat
pip install -e "../../[slack]" -r requirements.txt

copy config_template.json config.json   # Webhook URL・カテゴリ・キーワードを設定
python -m scripts.init_db
```

## Running Commands

すべてのコマンドは `paper_watch/` からモジュール実行する。

```bash
# 朝バッチ（タスクスケジューラで毎日実行を想定）
python -m scripts.batch_morning

# DB 初期化（冪等）
python -m scripts.init_db
```

## Running Tests

```bash
python -m unittest discover -s tests
python -m unittest tests.test_scorer
python -m unittest tests.test_rss_fetcher
```

## Architecture

### Data Flow

```
arXiv RSS Feed → rss_fetcher → all_papers
  → 重複排除（DB 照合）→ new_papers
  → scorer（キーワードマッチ）→ scored_papers
  → DB upsert（papers テーブル）
  → notifier → Slack
```

### Key Source Modules (`src/`)

| Module | Role |
|--------|------|
| `config.py` | `config.json` を読み込み、`CONFIG` として提供 |
| `logger.py` | `shared.logger` の再エクスポート |
| `data_store.py` | SQLite 接続 + CRUD ヘルパー再エクスポート |
| `rss_fetcher.py` | arXiv RSS フィード取得・パース |
| `scorer.py` | primary/secondary キーワードの重み付きマッチスコアリング |
| `translator.py` | Google Translate による英→日翻訳（キーワード・Abstract） |
| `notifier.py` | Slack Block Kit 通知（新着論文レポート） |

### Database (`data/paper_watch.db`)

SQLite with WAL mode. Tables:
- `papers` — 論文データ（UNIQUE: arxiv_id）
- `batch_run_log` — バッチ実行���グ

### External Dependencies

- **arXiv RSS** — `http://rss.arxiv.org/rss/{category}`
- **Slack** (`config.json > slack.webhook_url`) — Incoming Webhook
- **shared/** — 共通モジュール（config, db, slack, logger）
