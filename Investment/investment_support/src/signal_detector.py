"""
売買シグナル検出モジュール（スイング戦略）

EMA押し目スイングの条件ベース検出結果を daily_signals に保存する。
"""

from datetime import date
from typing import Optional

from src.config import CONFIG
from src.data_store import fetch_all, fetch_one, get_connection, upsert
from src.logger import get_logger
from src.swing_detector import PATTERN_SWING, evaluate_swing

logger = get_logger(__name__)

_SIG = CONFIG.get("signal", {})

# daily_signals に保存可能な列のみ upsert する（evaluate_swing の内部フィールドを除外）
_DAILY_SIGNALS_COLUMNS = frozenset({
    "ticker", "date", "market", "pattern", "label",
    "cond_a_ma200_rising", "cond_a_ma_aligned", "cond_a_52w_high_near",
    "cond_a_volume_surge", "cond_a_rsi_ok", "cond_a_above_ma25",
    "cond_b_ma200_rising", "cond_b_ma_aligned", "cond_b_pullback_touched",
    "cond_b_bounced", "cond_b_ma25_rising", "cond_b_rsi_ok",
    "cond_c_weekly_uptrend", "cond_c_at_support", "cond_c_rsi_oversold", "cond_c_sar_reversal",
    "close", "rsi14", "atr14", "vol_ratio", "rs_rank", "market_mode",
})


def _max_entry_sets_per_batch() -> int:
    value = _SIG.get(
        "max_entry_sets_per_batch",
        _SIG.get("max_candidates_per_pattern", 3),
    )
    return max(int(value), 1)


def detect_and_save_signals(
    screened_tickers: list[dict],
    target_date: date,
    market_mode: str,
) -> list[dict]:
    """流動性フィルタ通過銘柄にスイング検出を行い daily_signals に保存する。"""
    target_str = target_date.strftime("%Y-%m-%d")
    candidates: list[dict] = []
    all_rows: list[dict] = []
    data_insufficient_count = 0

    logger.info(f"市場環境 [{market_mode}]: スイングシグナル検出を実施")

    for tk in screened_tickers:
        if any(tk.get(f) is None for f in ("ema25", "ema75", "rsi14")):
            data_insufficient_count += 1

        row = evaluate_swing(tk, target_str)
        row["market_mode"] = market_mode

        if market_mode in ("caution", "full_caution") and row["label"] == "candidate":
            row["label"] = "watch"

        all_rows.append(row)
        if row["label"] == "candidate":
            row["_notify_extras"] = {
                fld: tk.get(fld)
                for fld in ("ma5", "ma25", "ma75", "ma200", "high_52w", "support_level")
            }
            candidates.append(row)

    candidates.sort(key=lambda x: (x.get("rsi14") or 999))
    for rank, c in enumerate(candidates, start=1):
        c["rs_rank"] = rank

    rank_map = {c["ticker"]: c["rs_rank"] for c in candidates}
    for row in all_rows:
        row["rs_rank"] = rank_map.get(row["ticker"])

    market = screened_tickers[0]["market"] if screened_tickers else None
    chunk = 500
    with get_connection() as conn:
        if market:
            cur = conn.execute(
                "DELETE FROM daily_signals WHERE date = ? AND market = ?",
                (target_str, market),
            )
            try:
                deleted = int(cur.rowcount)
            except (TypeError, ValueError):
                deleted = 0
            if deleted > 0:
                logger.info(
                    f"daily_signals 旧行を削除: {market} {target_str} "
                    f"({deleted:,} 件 → {len(all_rows):,} 件に置換)"
                )
        for i in range(0, len(all_rows), chunk):
            save_rows = [
                {
                    k: v
                    for k, v in r.items()
                    if not k.startswith("_") and k in _DAILY_SIGNALS_COLUMNS
                }
                for r in all_rows[i:i + chunk]
            ]
            if save_rows:
                upsert(conn, "daily_signals", save_rows)

    for c in candidates:
        extras = c.pop("_notify_extras", {})
        for fld, val in extras.items():
            if fld not in c or c[fld] is None:
                c[fld] = val

    candidate_count = len(candidates)
    watch_count = sum(1 for r in all_rows if r["label"] == "watch")
    insufficient_str = (
        f" / データ不足={data_insufficient_count} 件" if data_insufficient_count > 0 else ""
    )
    if market_mode in ("caution", "full_caution"):
        logger.info(
            f"シグナル検出完了: {len(screened_tickers)} 銘柄中 "
            f"watch={watch_count} 件（警戒モードのため新規買い候補は停止）{insufficient_str}"
        )
    else:
        logger.info(
            f"シグナル検出完了: {len(screened_tickers)} 銘柄中 "
            f"candidate={candidate_count} 件 / watch={watch_count} 件{insufficient_str}"
        )
    return candidates[:_max_entry_sets_per_batch()]


def diagnose_signal_save_count(
    target_date: date,
    market: str,
    expected_count: int,
) -> Optional[str]:
    """daily_signals の保存件数がスクリーニング件数と一致するか確認する。"""
    if expected_count <= 0:
        return None

    with get_connection() as conn:
        row = fetch_one(
            conn,
            "SELECT COUNT(*) AS c FROM daily_signals WHERE date = ? AND market = ?",
            (target_date.strftime("%Y-%m-%d"), market),
        )
    actual_count = int(row["c"]) if row else 0
    if actual_count == expected_count:
        return None
    return (
        f"daily_signals 保存件数不一致（{market} {target_date.isoformat()}）: "
        f"期待={expected_count:,} / 実績={actual_count:,}"
    )


def fetch_near_misses(
    target_date: date,
    market: str = "US",
    limit: int = 5,
) -> list[dict]:
    """監視リスト（watch）のうち RSI 条件まで満たす銘柄を返す。"""
    target_str = target_date.strftime("%Y-%m-%d")
    with get_connection() as conn:
        rows = fetch_all(
            conn,
            """
            SELECT d.ticker, u.name, d.market, d.close, d.rsi14,
                   d.cond_c_weekly_uptrend, d.cond_c_rsi_oversold, d.cond_c_sar_reversal,
                   t.entry_price, t.stop_loss_price
            FROM daily_signals d
            LEFT JOIN universe_master u ON u.ticker = d.ticker
            LEFT JOIN trade_signal_log t
              ON d.ticker = t.ticker AND d.date = t.signal_date
            WHERE d.date = ? AND d.market = ? AND d.label = 'watch'
              AND d.cond_c_weekly_uptrend = 1
              AND d.cond_c_rsi_oversold = 1
              AND d.cond_c_sar_reversal = 0
            ORDER BY d.rsi14 ASC
            LIMIT ?
            """,
            (target_str, market, limit),
        )
    result = []
    for r in rows:
        item = dict(r)
        item["pattern"] = PATTERN_SWING
        item["near_miss_type"] = "macd_pending"
        result.append(item)
    return result


fetch_watch_stocks = fetch_near_misses
