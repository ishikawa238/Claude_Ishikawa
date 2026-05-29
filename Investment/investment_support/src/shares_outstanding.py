"""
発行済株式数（shares_outstanding）の読み込み・更新。

- universe_master からテクニカル計算用の shares_map を構築
- US: EODHD fundamentals API で欠損分を補完（月次バッチ等で段階的に実行）
- JP: J-Quants は jquants_fetcher.fetch_and_save_fundamentals を利用
"""

import time
from datetime import date
from typing import Optional

from src.config import CONFIG
from src.data_store import fetch_all, get_connection
from src.logger import get_logger

logger = get_logger(__name__)

_SC = CONFIG.get("screening", {})
_US_REFRESH_ENABLED = bool(_SC.get("us_shares_refresh_enabled", True))
_US_REFRESH_MAX = int(_SC.get("us_shares_refresh_max_per_monthly", 200))
_US_REFRESH_INTERVAL = float(_SC.get("us_shares_refresh_interval_sec", 0.25))


def load_shares_map(tickers: list[str]) -> dict[str, float]:
    """
    universe_master から発行済株式数を読み込む（テクニカル計算の est_market_cap 用）。
    """
    if not tickers:
        return {}
    placeholders = ",".join(["?"] * len(tickers))
    with get_connection() as conn:
        rows = fetch_all(
            conn,
            f"SELECT ticker, shares_outstanding FROM universe_master "
            f"WHERE ticker IN ({placeholders}) AND shares_outstanding IS NOT NULL",
            tuple(tickers),
        )
    result: dict[str, float] = {}
    for r in rows:
        val = r["shares_outstanding"]
        if val is not None:
            result[r["ticker"]] = float(val)
    return result


def count_shares_known(market: str) -> int:
    """指定市場で shares_outstanding が登録されている銘柄数。"""
    with get_connection() as conn:
        row = fetch_all(
            conn,
            "SELECT COUNT(*) AS c FROM universe_master "
            "WHERE market = ? AND is_active = 1 AND is_excluded = 0 "
            "AND shares_outstanding IS NOT NULL",
            (market,),
        )
    return int(row[0]["c"]) if row else 0


def refresh_us_shares_outstanding(
    today: date,
    *,
    max_updates: Optional[int] = None,
) -> int:
    """
    shares_outstanding が未設定の US 銘柄を EODHD から取得して更新する。

    API コール数制限のため max_updates 件まで（デフォルト: config.screening）。
    返り値: 更新できた銘柄数
    """
    from src.data_fetcher import fetch_shares_outstanding

    if not _US_REFRESH_ENABLED:
        logger.info("US 発行済株式数: config で無効（us_shares_refresh_enabled=false）")
        return 0

    limit = max_updates if max_updates is not None else _US_REFRESH_MAX
    if limit <= 0:
        return 0

    today_str = today.strftime("%Y-%m-%d")
    with get_connection() as conn:
        rows = fetch_all(
            conn,
            "SELECT ticker FROM universe_master "
            "WHERE market = 'US' AND is_active = 1 AND is_excluded = 0 "
            "AND shares_outstanding IS NULL "
            "ORDER BY ticker LIMIT ?",
            (limit,),
        )
    tickers = [r["ticker"] for r in rows]
    if not tickers:
        logger.info("US 発行済株式数: 更新対象なし（欠損0件）")
        return 0

    updated = 0
    logger.info(f"US 発行済株式数: 最大 {limit} 件を EODHD から取得します（対象 {len(tickers)} 件）")

    for i, ticker in enumerate(tickers, start=1):
        try:
            shares = fetch_shares_outstanding(ticker, "US")
            if shares is None and i == 1:
                from src.data_fetcher import is_eodhd_fundamentals_disabled
                if is_eodhd_fundamentals_disabled():
                    logger.info(
                        "EODHD fundamentals 利用不可のため、残り %d 件の取得を中止します。",
                        len(tickers) - i + 1,
                    )
                    break
            if shares and shares > 0:
                with get_connection() as conn:
                    conn.execute(
                        "UPDATE universe_master SET shares_outstanding = ?, last_updated = ? "
                        "WHERE ticker = ?",
                        (shares, today_str, ticker),
                    )
                    conn.commit()
                updated += 1
            if i % 50 == 0:
                logger.info(f"  US 発行済株式数 進捗: {i}/{len(tickers)}（更新 {updated} 件）")
        except Exception as e:
            logger.warning(f"US 発行済株式数取得失敗: {ticker} ({e})")
        time.sleep(_US_REFRESH_INTERVAL)

    logger.info(f"US 発行済株式数更新完了: {updated}/{len(tickers)} 件")
    return updated
