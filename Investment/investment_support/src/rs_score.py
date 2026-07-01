"""
Relative Strength (RS) スコア — ベンチマーク対比パフォーマンスの百分位（0〜100）。

銘柄の N 日リターンからインデックス N 日リターンを引いた相対リターンを、
同一市場内の全銘柄で横断ランキングし 0〜100 に正規化する。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from src.config import get_rs_score_config
from src.data_store import fetch_all, get_connection
from src.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_BENCHMARK = {"JP": "N225", "US": "SP500"}


def _benchmark_for_market(market: str, cfg: dict) -> str:
    key = f"{market.lower()}_benchmark"
    return str(cfg.get(key) or _DEFAULT_BENCHMARK.get(market.upper(), "SP500"))


def index_return_pct(index_code: str, target_date: date, lookback: int) -> Optional[float]:
    """インデックスの lookback 営業日リターン（%）。"""
    target_str = target_date.strftime("%Y-%m-%d")
    from_str = (target_date - timedelta(days=max(lookback * 2, 400))).strftime("%Y-%m-%d")
    with get_connection() as conn:
        rows = fetch_all(
            conn,
            "SELECT date, close FROM index_snapshot "
            "WHERE index_code = ? AND date <= ? AND date >= ? "
            "ORDER BY date ASC",
            (index_code, target_str, from_str),
        )
    if len(rows) < lookback + 1:
        return None
    window = rows[-(lookback + 1):]
    start_close = float(window[0]["close"])
    end_close = float(window[-1]["close"])
    if start_close <= 0:
        return None
    return (end_close / start_close - 1.0) * 100.0


def percentile_scores(relative_returns: list[tuple[str, float]]) -> dict[str, float]:
    """相対リターンの順位を 0〜100 の RS スコアに変換する。"""
    if not relative_returns:
        return {}
    if len(relative_returns) == 1:
        return {relative_returns[0][0]: 50.0}
    sorted_items = sorted(relative_returns, key=lambda item: item[1])
    n = len(sorted_items)
    return {
        ticker: round(100.0 * rank / (n - 1), 2)
        for rank, (ticker, _) in enumerate(sorted_items)
    }


def update_rs_scores(market: str, target_date: date) -> int:
    """
    指定市場・日付の technical_snapshot.rs_score を一括更新する。

    返り値: 更新件数
    """
    cfg = get_rs_score_config()
    if not cfg.get("enabled", True):
        logger.info(f"RSスコア: 無効のためスキップ ({market})")
        return 0

    lookback = int(cfg.get("lookback_days", 126))
    benchmark = _benchmark_for_market(market, cfg)
    index_ret = index_return_pct(benchmark, target_date, lookback)
    if index_ret is None:
        logger.warning(
            f"RSスコア: {benchmark} の {lookback}日リターン未取得のためスキップ ({market})"
        )
        return 0

    target_str = target_date.strftime("%Y-%m-%d")
    with get_connection() as conn:
        rows = fetch_all(
            conn,
            """
            SELECT t.ticker, t.return_126d
            FROM technical_snapshot t
            JOIN universe_master u ON u.ticker = t.ticker
            WHERE t.date = ?
              AND u.market = ?
              AND t.return_126d IS NOT NULL
            """,
            (target_str, market),
        )

    relative = [
        (row["ticker"], float(row["return_126d"]) - index_ret)
        for row in rows
    ]
    scores = percentile_scores(relative)

    updated = 0
    with get_connection() as conn:
        for ticker, score in scores.items():
            conn.execute(
                "UPDATE technical_snapshot SET rs_score = ? WHERE ticker = ? AND date = ?",
                (score, ticker, target_str),
            )
            updated += 1
        conn.commit()

    logger.info(
        f"RSスコア更新: {market} {target_str} → {updated:,} 件 "
        f"(benchmark={benchmark}, index {lookback}d={index_ret:+.2f}%)"
    )
    return updated


def apply_rs_filter(rows: list[dict], cfg: dict | None = None) -> list[dict]:
    """filter_enabled 時に min_rs_score 未満の銘柄を除外する。"""
    cfg = cfg or get_rs_score_config()
    if not cfg.get("filter_enabled", False):
        return rows
    min_score = float(cfg.get("min_rs_score", 70))
    kept = [r for r in rows if (r.get("rs_score") or 0) >= min_score]
    if len(kept) < len(rows):
        logger.info(
            f"RS足切り: {len(rows):,} → {len(kept):,} 件 "
            f"(min_rs_score={min_score})"
        )
    return kept


def dynamic_rsi_threshold(
    rs_score: Optional[float],
    static_max: float,
    cfg: dict | None = None,
) -> float:
    """
    RSスコアに応じた RSI 上限を返す。

    式: base + (rs_score - anchor) * slope （上限 cap でクリップ）
    """
    cfg = cfg or get_rs_score_config()
    if not cfg.get("dynamic_rsi_enabled", True):
        return static_max
    if rs_score is None:
        return static_max
    base = float(cfg.get("dynamic_rsi_base", 35))
    anchor = float(cfg.get("dynamic_rsi_anchor_score", 70))
    slope = float(cfg.get("dynamic_rsi_slope", 0.5))
    cap = float(cfg.get("dynamic_rsi_max", 55))
    threshold = base + (float(rs_score) - anchor) * slope
    return min(max(threshold, base), cap)
