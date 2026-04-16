"""
初回履歴データ一括取得スクリプト

実行方法（investment_support/ ディレクトリから）:
    python -m scripts.fetch_historical

処理内容:
1. EODHD から日本株・米国株の銘柄マスタを取得して universe_master に保存
2. 過去3年分の日足価格データを yfinance で全銘柄分取得して price_history に保存
   （yfinance は無料・API キー不要。EODHD の日次制限を消費しない）
3. 市場インデックス4本の過去3年分を EODHD から取得して index_snapshot に保存
4. 全銘柄の technical_snapshot を計算して保存（最終日のみ）

注意:
- 日本株約4,000銘柄 + 米国株約8,000銘柄 × 3年分のため、完了まで数時間かかる場合がある
- yfinance は非公式 API のため将来的に動作しなくなる可能性がある
- 途中でエラーが出ても、再実行すると重複分は UPSERT で上書きされる（冪等）
"""

import sys
from datetime import date, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.config import CONFIG
from src.data_fetcher import (
    INDEX_TICKERS,
    fetch_exchange_symbols,
    fetch_index_history,
)
from src.data_store import fetch_all, get_connection, log_error, upsert
from src.logger import get_logger
from src.technical_calculator import (
    calculate_and_save_index_mas,
    calculate_and_save_snapshots,
)
from src.yfinance_fetcher import fetch_history_bulk_yf

logger = get_logger(__name__)

# ============================================================
# 設定
# ============================================================

_HISTORY_YEARS = int(CONFIG["initial_fetch"].get("history_years", 3))
_TODAY = date.today()
_FROM_DATE = _TODAY - timedelta(days=_HISTORY_YEARS * 365 + 30)  # 余裕を持って30日追加


# ============================================================
# ステップ1: 銘柄マスタ取得・保存
# ============================================================

def step1_fetch_universe() -> dict[str, list[dict]]:
    """
    日本株・米国株の銘柄マスタを取得して universe_master に保存する。
    既に universe_master にデータがある市場はスキップし、既存データを使う。
    """
    logger.info("=" * 60)
    logger.info("STEP 1: 銘柄マスタ取得")
    logger.info("=" * 60)

    all_symbols: dict[str, list[dict]] = {}

    for market in ["JP", "US"]:
        # 既存データ確認（is_excluded/is_active も取得して step2 で使えるようにする）
        with get_connection() as conn:
            existing = fetch_all(
                conn,
                "SELECT ticker, market, is_excluded, is_active FROM universe_master "
                "WHERE market=? AND is_active=1 AND is_excluded=0",
                (market,),
            )

        if existing:
            logger.info(f"  {market}: DB に {len(existing)} 銘柄が既存。スキップします。")
            all_symbols[market] = [dict(r) for r in existing]
            continue

        logger.info(f"  {market} 銘柄マスタ取得中（EODHD）...")
        symbols = fetch_exchange_symbols(market)
        if not symbols:
            logger.error(f"  {market} 銘柄マスタの取得に失敗しました。APIキーを確認してください。")
            continue

        with get_connection() as conn:
            upsert(conn, "universe_master", symbols)

        logger.info(f"  {market}: {len(symbols)} 銘柄を保存しました。")
        all_symbols[market] = symbols

    return all_symbols


# ============================================================
# ステップ2: 価格データ一括取得
# ============================================================

def step2_fetch_prices(all_symbols: dict[str, list[dict]]) -> None:
    """全銘柄の過去3年分の価格データを yfinance で取得して price_history に保存する。"""
    logger.info("=" * 60)
    logger.info(f"STEP 2: 価格データ一括取得（yfinance） ({_FROM_DATE} 〜 {_TODAY})")
    logger.info("=" * 60)

    for market, symbols in all_symbols.items():
        # 対象外銘柄（ETF等）はスキップ
        active_symbols = [s for s in symbols if s["is_excluded"] == 0 and s["is_active"] == 1]
        tickers_with_market = [(s["ticker"], market) for s in active_symbols]

        logger.info(f"  {market}: {len(tickers_with_market)} 銘柄の価格取得開始（yfinance）...")

        all_rows, failed = fetch_history_bulk_yf(
            tickers_with_market,
            from_date=_FROM_DATE,
            to_date=_TODAY,
        )

        if not all_rows:
            logger.warning(f"  {market}: 価格データが1件も取得できませんでした。")
            continue

        # 5000件ごとに分割して保存（大量データのメモリ節約）
        chunk_size = 5000
        total_saved = 0
        for i in range(0, len(all_rows), chunk_size):
            chunk = all_rows[i:i + chunk_size]
            with get_connection() as conn:
                upsert(conn, "price_history", chunk)
            total_saved += len(chunk)

        logger.info(
            f"  {market}: {total_saved} 件保存完了 "
            f"（失敗: {len(failed)} 銘柄）"
        )


# ============================================================
# ステップ3: インデックスデータ取得
# ============================================================

def step3_fetch_indices() -> None:
    """市場インデックス4本の価格データを取得して index_snapshot に保存する。"""
    logger.info("=" * 60)
    logger.info("STEP 3: 市場インデックスデータ取得")
    logger.info("=" * 60)

    for index_code in INDEX_TICKERS:
        logger.info(f"  {index_code} 取得中...")
        rows = fetch_index_history(index_code, _FROM_DATE, _TODAY)
        if not rows:
            logger.warning(f"  {index_code}: データ取得失敗。スキップします。")
            continue

        with get_connection() as conn:
            upsert(conn, "index_snapshot", rows)

        logger.info(f"  {index_code}: {len(rows)} 件保存完了")

        # MA を計算して index_snapshot を更新
        calculate_and_save_index_mas(index_code, _TODAY)


# ============================================================
# ステップ4: テクニカル指標計算（最終日のみ）
# ============================================================

def step4_calculate_technicals() -> None:
    """
    全銘柄の最終営業日のテクニカルスナップショットを計算して保存する。
    初回取得時は最新日のみ計算する（全日付分は計算コストが高すぎる）。
    """
    logger.info("=" * 60)
    logger.info(f"STEP 4: テクニカル指標計算 (対象日: {_TODAY})")
    logger.info("=" * 60)

    with get_connection() as conn:
        rows = fetch_all(
            conn,
            "SELECT ticker, market FROM universe_master "
            "WHERE is_active = 1 AND is_excluded = 0",
        )

    tickers_with_market = [(r["ticker"], r["market"]) for r in rows]
    logger.info(f"  対象銘柄数: {len(tickers_with_market)}")

    calculate_and_save_snapshots(tickers_with_market, _TODAY)


# ============================================================
# 最終サマリー表示
# ============================================================

def print_summary() -> None:
    """取得・保存結果のサマリーをコンソールに表示する。"""
    import io
    out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    def p(text: str = "") -> None:
        out.write(text + "\n")
        out.flush()

    with get_connection() as conn:
        universe_jp = fetch_all(conn, "SELECT COUNT(*) as c FROM universe_master WHERE market='JP' AND is_active=1")[0]["c"]
        universe_us = fetch_all(conn, "SELECT COUNT(*) as c FROM universe_master WHERE market='US' AND is_active=1")[0]["c"]
        price_rows  = fetch_all(conn, "SELECT COUNT(*) as c FROM price_history")[0]["c"]
        index_rows  = fetch_all(conn, "SELECT COUNT(*) as c FROM index_snapshot")[0]["c"]
        tech_rows   = fetch_all(conn, "SELECT COUNT(*) as c FROM technical_snapshot")[0]["c"]

    p("\n" + "=" * 60)
    p("  初回データ取得完了サマリー")
    p("=" * 60)
    p(f"  取得期間         : {_FROM_DATE} 〜 {_TODAY}")
    p(f"  銘柄マスタ（JP） : {universe_jp:>6} 銘柄")
    p(f"  銘柄マスタ（US） : {universe_us:>6} 銘柄")
    p(f"  価格データ       : {price_rows:>8} 件")
    p(f"  インデックス     : {index_rows:>8} 件")
    p(f"  テクニカル指標   : {tech_rows:>8} 件")
    p("=" * 60)
    p("  次のステップ: Windowsタスクスケジューラに以下を登録してください")
    p("    平日 20:00 → python -m scripts.batch_evening_jp")
    p("    平日 07:00 → python -m scripts.batch_morning_all")
    p("=" * 60 + "\n")


# ============================================================
# エントリーポイント
# ============================================================

if __name__ == "__main__":
    logger.info("初回データ取得スクリプト開始")
    logger.info(f"取得期間: {_FROM_DATE} 〜 {_TODAY}")

    try:
        all_symbols = step1_fetch_universe()
        if not all_symbols:
            logger.error("銘柄マスタが1件も取得できませんでした。APIキーを確認してください。")
            sys.exit(1)
        missing = [m for m in ["JP", "US"] if m not in all_symbols]
        if missing:
            logger.warning(f"以下の市場は銘柄マスタ未取得のためスキップします: {missing}")

        step2_fetch_prices(all_symbols)
        step3_fetch_indices()
        step4_calculate_technicals()
        print_summary()

    except KeyboardInterrupt:
        logger.info("ユーザーによる中断。途中まで取得したデータは保存済みです。")
        sys.exit(0)
    except Exception as e:
        logger.error(f"予期しないエラーが発生しました: {e}", exc_info=True)
        log_error(
            batch_type="fetch_historical",
            module="fetch_historical",
            error_type=type(e).__name__,
            error_message=str(e),
            is_fatal=True,
        )
        sys.exit(1)
