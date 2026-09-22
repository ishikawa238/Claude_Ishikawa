"""
一時キャッチアップ: 滞留後のデータ再取得・再計算。
moomoo OpenD 未接続でも完走するよう同期処理を無効化する。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

from src.data_store import fetch_all, fetch_one, get_connection, upsert
from src.logger import get_logger

logger = get_logger("catchup_refresh")
TODAY = date.today()


def _disable_moomoo() -> None:
    import src.moomoo_sync as ms
    import src.portfolio_snapshot as ps

    ms.sync_from_moomoo = lambda *a, **k: (0, 0)
    ms.sync_trade_results = lambda *a, **k: 0
    ms.fetch_account_cash = lambda: {"cash_jpy": 0.0, "cash_usd": 0.0}
    ps._fetch_moomoo_cash = lambda: {"cash_jpy": 0.0, "cash_usd": 0.0}
    logger.info("moomoo 同期を無効化して続行します")


def _progress(step: int, total: int, label: str) -> None:
    logger.info(f"[progress] {step}/{total} {label}")


def run_jp_evening() -> int:
    from scripts.batch_evening_jp import run_manual

    logger.info("=== JP evening catchup 開始 ===")
    rc = run_manual(skip_llm=True, on_progress=_progress)
    logger.info(f"=== JP evening catchup 終了 rc={rc} ===")
    return rc


def backfill_missing_tech_days(market: str, days: list[date]) -> None:
    """価格はあるが tech が無い日を埋める。"""
    from src.shares_outstanding import load_shares_map
    from src.technical_calculator import calculate_and_save_snapshots
    from src.rs_score import update_rs_scores
    from src.market_filter import judge_market_environment, get_screened_tickers
    from src.signal_detector import detect_and_save_signals
    from src.entry_calculator import save_entry_sets

    for d in days:
        with get_connection() as conn:
            px = fetch_one(
                conn,
                "SELECT COUNT(*) c FROM price_history p "
                "JOIN universe_master u ON u.ticker=p.ticker "
                "WHERE u.market=? AND u.is_active=1 AND p.date=?",
                (market, d.isoformat()),
            )["c"]
            tech = fetch_one(
                conn,
                "SELECT COUNT(*) c FROM technical_snapshot t "
                "JOIN universe_master u ON u.ticker=t.ticker "
                "WHERE u.market=? AND t.date=?",
                (market, d.isoformat()),
            )["c"]
        if px < 100:
            logger.info(f"skip tech {market} {d}: price={px}")
            continue
        if tech >= px * 0.9:
            logger.info(f"skip tech {market} {d}: already tech={tech}")
            continue

        with get_connection() as conn:
            priced = fetch_all(
                conn,
                "SELECT p.ticker FROM price_history p "
                "JOIN universe_master u ON u.ticker=p.ticker "
                "WHERE u.market=? AND u.is_active=1 AND u.is_excluded=0 AND p.date=?",
                (market, d.isoformat()),
            )
        twm = [(r["ticker"], market) for r in priced]
        logger.info(f"tech backfill {market} {d}: {len(twm)} tickers")
        shares_map = load_shares_map([t for t, _ in twm])
        calculate_and_save_snapshots(twm, d, shares_map=shares_map)
        update_rs_scores(market, d)
        env = judge_market_environment(d, market=market)
        mode = env.get("mode", "caution")
        screened = get_screened_tickers(d, market)
        cands = detect_and_save_signals(screened, d, mode)
        save_entry_sets(cands, mode)
        logger.info(
            f"tech backfill done {market} {d}: screened={len(screened)} "
            f"candidates={len(cands)} mode={mode}"
        )


def run_us_yfinance_catchup() -> None:
    """EODHD 401 時の US 補完: 指数 + 直近欠損銘柄を yfinance で埋める。"""
    from src.yfinance_fetcher import fetch_daily_yf, fetch_index_history_yf, fetch_history_bulk_yf
    from src.technical_calculator import calculate_and_save_index_mas, calculate_and_save_snapshots
    from src.shares_outstanding import load_shares_map
    from src.rs_score import update_rs_scores
    from src.market_filter import judge_market_environment, get_screened_tickers
    from src.signal_detector import detect_and_save_signals
    from src.entry_calculator import save_entry_sets

    logger.info("=== US yfinance catchup 開始 ===")
    for code in ("SP500", "NASDAQ"):
        rows = fetch_index_history_yf(code, TODAY - timedelta(days=10), TODAY)
        if rows:
            with get_connection() as conn:
                upsert(conn, "index_snapshot", rows)
            logger.info(f"index {code}: {len(rows)} rows")

    # 直近の「十分な」US価格日を基準に、その後の営業日で欠けている銘柄を補完
    with get_connection() as conn:
        base = fetch_one(
            conn,
            """
            SELECT p.date AS d, COUNT(*) AS c
            FROM price_history p
            JOIN universe_master u ON u.ticker=p.ticker
            WHERE u.market='US' AND u.is_active=1 AND p.date >= date('now','-14 days')
            GROUP BY p.date
            HAVING c >= 10000
            ORDER BY p.date DESC
            LIMIT 1
            """,
        )
    if not base:
        logger.warning("US 基準日が見つかりません")
        return

    base_date = date.fromisoformat(base["d"])
    logger.info(f"US base date {base_date} count={base['c']}")

    with get_connection() as conn:
        base_tickers = {
            r["ticker"]
            for r in fetch_all(
                conn,
                "SELECT p.ticker FROM price_history p "
                "JOIN universe_master u ON u.ticker=p.ticker "
                "WHERE u.market='US' AND u.is_active=1 AND u.is_excluded=0 AND p.date=?",
                (base_date.isoformat(),),
            )
        }

    # 直近営業日候補（土日除外の簡易）
    targets = []
    d = base_date + timedelta(days=1)
    while d <= TODAY:
        if d.weekday() < 5:
            targets.append(d)
        d += timedelta(days=1)

    for td in targets:
        with get_connection() as conn:
            have = {
                r["ticker"]
                for r in fetch_all(
                    conn,
                    "SELECT ticker FROM price_history WHERE date=?",
                    (td.isoformat(),),
                )
            }
        missing = sorted(base_tickers - have)
        logger.info(f"US {td}: have={len(have & base_tickers)} missing={len(missing)}")
        if not missing:
            continue
        # 一度にやりすぎないようチャンク
        chunk = 3000
        all_rows: list[dict] = []
        for i in range(0, len(missing), chunk):
            part = missing[i : i + chunk]
            twm = [(t, "US") for t in part]
            rows, failed = fetch_history_bulk_yf(
                twm, td - timedelta(days=5), td
            )
            logger.info(
                f"  chunk {i}-{i+len(part)}: rows={len(rows)} failed={len(failed)}"
            )
            all_rows.extend(rows)
        if all_rows:
            # 日次保存は全期間行を入れる（欠落埋め）
            with get_connection() as conn:
                upsert(conn, "price_history", all_rows)
            logger.info(f"US {td}: saved {len(all_rows)} price rows")

    # 最新の十分な価格日で tech / signals
    with get_connection() as conn:
        latest = fetch_one(
            conn,
            """
            SELECT p.date AS d, COUNT(*) AS c
            FROM price_history p
            JOIN universe_master u ON u.ticker=p.ticker
            WHERE u.market='US' AND u.is_active=1 AND p.date >= date('now','-10 days')
            GROUP BY p.date
            HAVING c >= 8000
            ORDER BY p.date DESC
            LIMIT 1
            """,
        )
    if not latest:
        logger.warning("US tech 対象日なし")
        return

    us_date = date.fromisoformat(latest["d"])
    logger.info(f"US tech/signals on {us_date} (n={latest['c']})")
    calculate_and_save_index_mas("SP500", us_date)
    calculate_and_save_index_mas("NASDAQ", us_date)
    with get_connection() as conn:
        priced = fetch_all(
            conn,
            "SELECT p.ticker FROM price_history p "
            "JOIN universe_master u ON u.ticker=p.ticker "
            "WHERE u.market='US' AND u.is_active=1 AND u.is_excluded=0 AND p.date=?",
            (us_date.isoformat(),),
        )
    twm = [(r["ticker"], "US") for r in priced]
    shares_map = load_shares_map([t for t, _ in twm])
    calculate_and_save_snapshots(twm, us_date, shares_map=shares_map)
    update_rs_scores("US", us_date)
    env = judge_market_environment(us_date, market="US")
    mode = env.get("mode", "caution")
    # 統合判定も更新
    judge_market_environment(us_date)
    screened = get_screened_tickers(us_date, "US")
    cands = detect_and_save_signals(screened, us_date, mode)
    save_entry_sets(cands, mode)
    logger.info(
        f"US done: mode={mode} screened={len(screened)} candidates={len(cands)}"
    )


def print_freshness() -> None:
    with get_connection() as conn:
        checks = [
            (
                "price_JP",
                "SELECT MAX(p.date) v FROM price_history p "
                "JOIN universe_master u ON u.ticker=p.ticker WHERE u.market='JP'",
            ),
            (
                "price_US",
                "SELECT MAX(p.date) v FROM price_history p "
                "JOIN universe_master u ON u.ticker=p.ticker WHERE u.market='US'",
            ),
            ("technical", "SELECT MAX(date) v FROM technical_snapshot"),
            ("signals", "SELECT MAX(date) v FROM daily_signals"),
            (
                "N225",
                "SELECT MAX(date) v FROM index_snapshot WHERE index_code='N225'",
            ),
            (
                "SP500",
                "SELECT MAX(date) v FROM index_snapshot WHERE index_code='SP500'",
            ),
            (
                "market_env",
                "SELECT date||' '||mode FROM market_environment ORDER BY date DESC LIMIT 1",
            ),
        ]
        for label, sql in checks:
            if "||" in sql:
                row = fetch_one(conn, "SELECT date, mode FROM market_environment ORDER BY date DESC LIMIT 1")
                print(f"{label}: {dict(row) if row else None}")
            else:
                print(f"{label}: {fetch_one(conn, sql)['v']}")
        print("--- coverage ---")
        for d in ["2026-09-17", "2026-09-18", "2026-09-19", "2026-09-21", "2026-09-22"]:
            r = fetch_one(
                conn,
                """
                SELECT ? AS d,
                  (SELECT COUNT(*) FROM price_history p JOIN universe_master u ON u.ticker=p.ticker AND u.market='JP' AND u.is_active=1 WHERE p.date=?) jp_px,
                  (SELECT COUNT(*) FROM technical_snapshot t JOIN universe_master u ON u.ticker=t.ticker AND u.market='JP' WHERE t.date=?) jp_tech,
                  (SELECT COUNT(*) FROM daily_signals WHERE date=? AND market='JP') jp_sig,
                  (SELECT COUNT(*) FROM price_history p JOIN universe_master u ON u.ticker=p.ticker AND u.market='US' AND u.is_active=1 WHERE p.date=?) us_px,
                  (SELECT COUNT(*) FROM technical_snapshot t JOIN universe_master u ON u.ticker=t.ticker AND u.market='US' WHERE t.date=?) us_tech,
                  (SELECT COUNT(*) FROM daily_signals WHERE date=? AND market='US') us_sig
                """,
                (d, d, d, d, d, d, d),
            )
            print(dict(r))


def main() -> int:
    _disable_moomoo()
    jp_rc = run_jp_evening()
    # 9/17 抜け埋め（JP/US）
    backfill_missing_tech_days("JP", [date(2026, 9, 17)])
    backfill_missing_tech_days("US", [date(2026, 9, 17)])
    run_us_yfinance_catchup()
    print("=== FRESHNESS ===")
    print_freshness()
    return 0 if jp_rc in (0, 1) else jp_rc


if __name__ == "__main__":
    raise SystemExit(main())
