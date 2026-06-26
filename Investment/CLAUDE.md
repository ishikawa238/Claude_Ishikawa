# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Investment decision support system for Japanese and US stocks. The system detects buy/sell patterns and sends Slack notifications; the user makes final trading decisions. All commands run from the `investment_support/` directory.

## Setup

```bash
cd investment_support
python -m venv .venv
.venv\Scripts\activate.bat       # Windows cmd
pip install -r requirements.txt

copy config_template.json config.json   # then fill in API keys
python -m scripts.init_db
python -m scripts.fetch_historical      # initial data load (takes several hours)
```

`yfinance` is listed in `requirements.txt` and is used for JP daily prices and the initial 3-year history load (`fetch_historical.py`, `yfinance_fetcher.py`).

## Running Commands

All commands must be run from `investment_support/` as module invocations:

```bash
# Daily batches (registered in Windows Task Scheduler)
python -m scripts.batch_evening_jp      # daily 20:30 — JP stocks
python -m scripts.batch_morning_all     # daily 07:30 — US stocks + morning report

# One-off / maintenance
python -m scripts.batch_monthly         # first business day of month
python -m scripts.fetch_historical      # re-run to backfill missing data (idempotent)
python -m scripts.add_trade_result      # manually record trade outcome

# Report server (for Slack report links)
python -m scripts.order_server          # starts Flask on 127.0.0.1:5100
```

## Running Tests

```bash
# From investment_support/
python -m unittest discover -s tests

# Single test file
python -m unittest tests.test_notifier
```

## Architecture

### Data Flow

```
External APIs → price_history / index_snapshot (SQLite)
             → technical_snapshot (calculated by technical_calculator.py)
             → market_environment (judged by market_filter.py)
             → daily_signals / trade_signal_log (detected by signal_detector.py + entry_calculator.py)
             → Slack notification (notifier.py)
             → Report link → order_server.py (/report page)
```

### Key Source Modules (`src/`)

| Module | Role |
|--------|------|
| `config.py` | Loads `config.json`; all other modules import `CONFIG` from here |
| `data_store.py` | SQLite abstraction — `get_connection()`, `upsert()`, `fetch_all()`, `fetch_one()`, `insert_one()`, `log_error()` |
| `data_fetcher.py` | EODHD API — bulk EOD (1 request/exchange/day), index history, symbol master |
| `yfinance_fetcher.py` | yfinance — JP daily prices + initial 3-year history load (free, unofficial API) |
| `jquants_fetcher.py` | J-Quants API — JP fundamentals and earnings schedule |
| `technical_calculator.py` | Computes MA5/25/75/200, EMA25/75, RSI14, ATR14, MACD, volatility, turnover, support/resistance → `technical_snapshot` |
| `market_filter.py` | Market environment judge (normal/caution/full_caution) + liquidity screening |
| `swing_detector.py` | EMA pullback swing condition evaluation (watch/candidate) |
| `signal_detector.py` | Swing signal detection → `daily_signals` |
| `entry_calculator.py` | Computes entry price, stop-loss, target, recommended amount → `trade_signal_log` |
| `holdings_monitor.py` | Evaluates stop-loss hits, take-profit target reaches, and earnings alerts for open positions |
| `take_profit.py` | Take-profit target management — type determination (I/II/III), T1/T2/T3 price calculation, reach detection, DB ops → `take_profit_targets` |
| `notifier.py` | Builds and sends Slack messages (morning and evening reports) |
| `llm_advisor.py` | Sends stock data + news to OpenAI/Anthropic/Gemini APIs and returns feedback for Slack notifications (config: `llm` section) |
| `news_fetcher.py` | Fetches market and ticker-specific news from Google News RSS for LLM prompt enrichment |
| `portfolio_snapshot.py` | Computes and persists total assets (holdings value + moomoo cash) per batch run → `portfolio_snapshot` |
| `report_generator.py` | Monthly Slack report |
| `logger.py` | Logging setup — use `get_logger(__name__)` in every module; outputs to console + `logs/batch_YYYYMMDD.log` |

### Batch Scripts (`scripts/`)

- `batch_evening_jp.py` — JP price fetch → technicals → market env → signals → entry sets → holdings check → portfolio snapshot → LLM advisor → Slack → DB backup
- `batch_morning_all.py` — US price fetch → technicals → market env → US signals → merge JP signals from prior evening → holdings check → portfolio snapshot → LLM advisor → Slack
- `batch_monthly.py` — Universe master refresh + J-Quants fundamentals + monthly Slack report
- `order_server.py` — Flask server that serves the daily report page (`/report`), rules page (`/rules`), settings page (`/settings`). Uses Jinja2 templates (`templates/`) and static files (`static/css/`, `static/js/`). Settings page allows editing capital/signal/screening/paper_mode values in `config.json` via `GET/POST /api/settings`. Additional API endpoints: `/api/portfolio-risk` (portfolio-level VaR/Beta/Sharpe), `/api/earnings-calendar` (30-day earnings timeline for holdings), `/api/system-health` (batch status, error count, data freshness).

### Database (`data/investment.db`)

SQLite with WAL mode. Tables:
- `universe_master` — stock master (JP/US)
- `price_history` — daily OHLCV per ticker
- `index_snapshot` — daily index prices + MAs (N225, TOPIX, SP500, NASDAQ)
- `technical_snapshot` — daily technical indicators per ticker
- `market_environment` — daily mode per date (normal/caution/full_caution)
- `holdings` — open/closed positions (source of truth is `holdings.csv`, synced via `sync_holdings_from_csv()`)
- `daily_signals` — all screened tickers with pattern condition results
- `trade_signal_log` — notified entry sets (entry/stop/target/amount)
- `take_profit_targets` — staged take-profit targets per holding (T1/T2/T3 prices, reach status, adjusted stop-loss)
- `trade_result_log` — trade outcomes (moomoo auto-sync or dashboard form or CLI)
- `portfolio_snapshot` — total assets snapshot per batch run (holdings value, moomoo cash, unrealized P&L, USD/JPY rate)
- `earnings_schedule` — JP earnings announcement dates from J-Quants
- `fundamentals_snapshot` — JP financial data from J-Quants
- `llm_feedback_log` — LLM advisor responses (date, provider, model, tokens, response text)
- `batch_run_log`, `report_log`, `error_log` — operational logs

`init_db.py` is idempotent (uses `IF NOT EXISTS`). Schema migrations are in `MIGRATION_STATEMENTS` using `try/except` for idempotency.

### External Dependencies

- **EODHD** (`config.json > eodhd.api_key`) — daily EOD price data. Free plan allows ~20 calls/day; the bulk endpoint covers all exchange tickers in 1 call.
- **J-Quants** (`config.json > jquants.api_key`) — JP fundamentals and earnings; uses refresh token.
- **Slack** (`config.json > slack.webhook_url`) — Incoming Webhook for all notifications.
- **OpenAI** (`config.json > llm.openai.api_key`, optional) — LLM advisor feedback via Chat Completions API. Disabled by default (`llm.enabled: false`).
- **Anthropic** (`config.json > llm.anthropic.api_key`, optional) — LLM advisor feedback via Messages API. Disabled by default.

### Holdings CSV

`investment_support/holdings.csv` is the primary source for holdings. Fields: `ticker, name, market, entry_price, entry_date, quantity, stop_loss_price, memo`. It is synced into the `holdings` table at each batch run via `sync_holdings_from_csv()`.
