# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Investment decision support system for Japanese and US stocks. This is a **semi-automated** system — it detects buy/sell patterns and sends Slack notifications with order confirmation links; the user makes final trading decisions. All commands run from the `investment_support/` directory.

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

`moomoo` SDK (`futu-api`) is an optional dependency required only for order execution and position sync. It is not listed in `requirements.txt`. `yfinance` is also not in `requirements.txt` but is required for the initial historical data load (`fetch_historical.py`); install it manually: `pip install yfinance`.

## Running Commands

All commands must be run from `investment_support/` as module invocations:

```bash
# Daily batches (registered in Windows Task Scheduler)
python -m scripts.batch_evening_jp      # weekdays 20:00 — JP stocks
python -m scripts.batch_morning_all     # weekdays 07:00 — US stocks + morning report

# One-off / maintenance
python -m scripts.batch_monthly         # first business day of month
python -m scripts.fetch_historical      # re-run to backfill missing data (idempotent)
python -m scripts.sync_from_moomoo      # sync holdings.csv from moomoo (requires OpenD)
python -m scripts.sync_from_moomoo --dry-run  # preview without writing
python -m scripts.add_trade_result      # manually record trade outcome

# Order confirmation server (for Slack order links)
python -m scripts.order_server          # starts Flask on 127.0.0.1:5100
```

## Running Tests

```bash
# From investment_support/
python -m unittest discover -s tests

# Single test file
python -m unittest tests.test_order_executor
```

## Architecture

### Data Flow

```
External APIs → price_history / index_snapshot (SQLite)
             → technical_snapshot (calculated by technical_calculator.py)
             → market_environment (judged by market_filter.py)
             → daily_signals / trade_signal_log (detected by signal_detector.py + entry_calculator.py)
             → Slack notification (notifier.py)
             → Order link → order_server.py → order_executor.py → moomoo OpenD
```

### Key Source Modules (`src/`)

| Module | Role |
|--------|------|
| `config.py` | Loads `config.json`; all other modules import `CONFIG` from here |
| `data_store.py` | SQLite abstraction — `get_connection()`, `upsert()`, `fetch_all()`, `fetch_one()`, `insert_one()`, `log_error()` |
| `data_fetcher.py` | EODHD API — bulk EOD (1 request/exchange/day), index history, symbol master |
| `yfinance_fetcher.py` | yfinance — initial 3-year history load only (free, no API limit) |
| `jquants_fetcher.py` | J-Quants API — JP fundamentals and earnings schedule |
| `technical_calculator.py` | Computes MA5/25/75/200, RSI14, ATR14, volatility, turnover → `technical_snapshot` |
| `market_filter.py` | Market environment judge (normal/caution/full_caution) + liquidity screening |
| `signal_detector.py` | Pattern A (breakout) and Pattern B (MA25 pullback) detection |
| `entry_calculator.py` | Computes entry price, stop-loss, target, recommended amount → `trade_signal_log` |
| `holdings_monitor.py` | Evaluates stop-loss hits and earnings alerts for open positions |
| `notifier.py` | Builds and sends Slack messages (morning and evening reports) |
| `order_executor.py` | moomoo market order execution via OpenD; HMAC URL signing |
| `report_generator.py` | Monthly Slack report |
| `logger.py` | Logging setup — use `get_logger(__name__)` in every module; outputs to console + `logs/batch_YYYYMMDD.log` |

### Batch Scripts (`scripts/`)

- `batch_evening_jp.py` — JP price fetch → technicals → market env → signals → entry sets → holdings check → Slack → DB backup
- `batch_morning_all.py` — US price fetch → technicals → market env → US signals → merge JP signals from prior evening → holdings check → Slack
- `batch_monthly.py` — Universe master refresh + J-Quants fundamentals + monthly Slack report
- `order_server.py` — Flask server that renders order confirmation HTML and calls `order_executor.place_order()`

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
- `trade_result_log` — manually recorded trade outcomes
- `earnings_schedule` — JP earnings announcement dates from J-Quants
- `fundamentals_snapshot` — JP financial data from J-Quants
- `batch_run_log`, `report_log`, `error_log` — operational logs

`init_db.py` is idempotent (uses `IF NOT EXISTS`). Schema migrations are in `MIGRATION_STATEMENTS` using `try/except` for idempotency.

### External Dependencies

- **EODHD** (`config.json > eodhd.api_key`) — daily EOD price data. Free plan allows ~20 calls/day; the bulk endpoint covers all exchange tickers in 1 call.
- **J-Quants** (`config.json > jquants.api_key`) — JP fundamentals and earnings; uses refresh token.
- **Slack** (`config.json > slack.webhook_url`) — Incoming Webhook for all notifications.
- **moomoo OpenD** — local daemon required for order execution and `sync_from_moomoo`. GUI version of OpenD disables the API unlock call; users must manually unlock trading in the moomoo UI before executing orders.

### Holdings CSV

`investment_support/holdings.csv` is the primary source for holdings. Fields: `ticker, name, market, entry_price, entry_date, quantity, stop_loss_price, memo`. It is synced into the `holdings` table at each batch run via `sync_holdings_from_csv()`. Stop-loss prices are **never** overwritten during moomoo sync.

### Order Security

Order confirmation URLs contain an HMAC-SHA256 signature (`order_server.secret` in config). The `order_server` must be running locally before clicking Slack order links.
