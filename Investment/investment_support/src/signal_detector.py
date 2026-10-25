"""
売買シグナル検出モジュール

スイング・ブレイクアウト等の戦略ごとに検出し daily_signals に保存する。
"""

from datetime import date
from typing import Optional

from src.config import (
    breakout_strategy_name,
    get_breakout_config,
    get_market_strategy,
    is_breakout_enabled,
)
from src.data_store import fetch_all, fetch_one, get_connection, upsert
from src.logger import get_logger
from src.breakout_detector import evaluate_breakout
from src.swing_detector import PATTERN_SWING, evaluate_swing

logger = get_logger(__name__)

from src.config import CONFIG

_SIG = CONFIG.get("signal", {})

_DAILY_SIGNALS_COLUMNS = frozenset({
    "ticker", "date", "market", "strategy", "pattern", "label",
    "cond_a_ma200_rising", "cond_a_ma_aligned", "cond_a_52w_high_near",
    "cond_a_volume_surge", "cond_a_rsi_ok", "cond_a_above_ma25",
    "cond_b_ma200_rising", "cond_b_ma_aligned", "cond_b_pullback_touched",
    "cond_b_bounced", "cond_b_ma25_rising", "cond_b_rsi_ok",
    "cond_c_weekly_uptrend", "cond_c_at_support", "cond_c_rsi_oversold", "cond_c_sar_reversal",
    "macd_confirm_type",
    "close", "rsi14", "atr14", "vol_ratio", "rs_rank", "rs_score", "market_mode",
})


def _max_entry_sets_per_batch() -> int:
    value = _SIG.get(
        "max_entry_sets_per_batch",
        _SIG.get("max_candidates_per_pattern", 3),
    )
    return max(int(value), 1)


def _breakout_max_candidates() -> int:
    return max(int(get_breakout_config().get("max_candidates_per_batch", 1)), 0)


def _candidate_max_atr_pct() -> float:
    """候補に許容する ATR÷株価 の上限（%）。0 以下で無効。"""
    return float(_SIG.get("candidate_max_atr_pct", 6.0))


def _atr_pct(row: dict) -> Optional[float]:
    """ATR14 を株価に対する割合（%）で返す。算出できなければ None。"""
    atr = row.get("atr14")
    close = row.get("close")
    if not atr or not close or float(close) <= 0:
        return None
    return float(atr) / float(close) * 100


def _volatility_sort_key(row: dict) -> tuple:
    """ボラティリティが小さい順。算出できない行は後ろに回す。"""
    pct = _atr_pct(row)
    return (pct is None, pct if pct is not None else 0.0, row.get("ticker") or "")


def _enabled_strategies(market: str) -> list[tuple[str, bool]]:
    """(strategy_name, is_swing) のリスト。"""
    strategies: list[tuple[str, bool]] = [
        (get_market_strategy(market), True),
    ]
    if is_breakout_enabled():
        strategies.append((breakout_strategy_name(market), False))
    return strategies


def _evaluate_strategy(
    screened_tickers: list[dict],
    target_str: str,
    market_mode: str,
    strategy_name: str,
    *,
    is_swing: bool,
) -> tuple[list[dict], list[dict], int]:
    """1戦略分を評価。返り値: (candidates, all_rows, data_insufficient_count)。"""
    candidates: list[dict] = []
    all_rows: list[dict] = []
    insufficient = 0
    max_atr_pct = _candidate_max_atr_pct()
    too_volatile = 0

    for tk in screened_tickers:
        if is_swing:
            if any(tk.get(f) is None for f in ("ema25", "ema75", "rsi14")):
                insufficient += 1
            row = evaluate_swing(
                tk, target_str, strategy=strategy_name,
                rsi_history=tk.get("rsi_history"),
            )
        else:
            if any(tk.get(f) is None for f in ("close", "high_60d_prior", "volume")):
                insufficient += 1
            row = evaluate_breakout(tk, target_str, strategy=strategy_name)

        row["market_mode"] = market_mode
        row["strategy"] = strategy_name

        if market_mode in ("caution", "full_caution") and row["label"] == "candidate":
            row["label"] = "watch"

        # 高ボラティリティ銘柄は損切り幅を管理できず、実績でも大きく負け越すため候補から外す
        if row["label"] == "candidate" and max_atr_pct > 0:
            pct = _atr_pct(row)
            if pct is not None and pct > max_atr_pct:
                row["label"] = "watch"
                too_volatile += 1

        all_rows.append(row)
        if row["label"] == "candidate":
            row["_notify_extras"] = {
                fld: tk.get(fld)
                for fld in ("ma5", "ma25", "ma75", "ma200", "high_52w", "support_level", "high_60d_prior")
            }
            candidates.append(row)

    # ボラティリティが小さい順。相対強度・出来高比の降順は、実績で順位が上がるほど
    # 成績が悪化する逆相関だったため使わない（docs/signal_expectancy_analysis.md）。
    candidates.sort(key=_volatility_sort_key)

    if too_volatile:
        logger.info(
            f"高ボラティリティのため候補から除外: {strategy_name} {too_volatile} 件"
            f"（ATR÷株価 > {max_atr_pct:g}%）"
        )

    for rank, c in enumerate(candidates, start=1):
        c["rs_rank"] = rank
    rank_map = {c["ticker"]: c["rs_rank"] for c in candidates}
    for row in all_rows:
        row["rs_rank"] = rank_map.get(row["ticker"])

    return candidates, all_rows, insufficient


def select_notify_candidates(
    swing_candidates: list[dict],
    breakout_candidates: list[dict],
) -> list[dict]:
    """通知枠に収まる候補を返す（スイング優先、ブレイクアウトは専用枠のみ）。"""
    swing_cap = max(_max_entry_sets_per_batch() - _breakout_max_candidates(), 1)
    breakout_cap = _breakout_max_candidates()
    selected = list(swing_candidates[:swing_cap])
    if breakout_cap > 0:
        selected.extend(breakout_candidates[:breakout_cap])
    return selected


def detect_and_save_signals(
    screened_tickers: list[dict],
    target_date: date,
    market_mode: str,
) -> list[dict]:
    """流動性フィルタ通過銘柄に各戦略の検出を行い daily_signals に保存する。"""
    target_str = target_date.strftime("%Y-%m-%d")
    market = screened_tickers[0]["market"] if screened_tickers else None
    strategies = _enabled_strategies(market or "JP")

    logger.info(
        f"市場環境 [{market_mode}]: シグナル検出を実施 "
        f"({', '.join(s[0] for s in strategies)})"
    )

    all_rows: list[dict] = []
    notify_candidates: list[dict] = []
    total_insufficient = 0

    swing_candidates: list[dict] = []
    breakout_candidates: list[dict] = []

    for strategy_name, is_swing in strategies:
        cands, rows, insufficient = _evaluate_strategy(
            screened_tickers, target_str, market_mode, strategy_name, is_swing=is_swing,
        )
        all_rows.extend(rows)
        total_insufficient += insufficient
        if is_swing:
            swing_candidates = cands
        else:
            breakout_candidates = cands

    notify_candidates.extend(
        select_notify_candidates(swing_candidates, breakout_candidates)
    )

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

    for c in notify_candidates:
        extras = c.pop("_notify_extras", {})
        for fld, val in extras.items():
            if fld not in c or c[fld] is None:
                c[fld] = val

    candidate_count = sum(1 for r in all_rows if r["label"] == "candidate")
    watch_count = sum(1 for r in all_rows if r["label"] == "watch")
    insufficient_str = (
        f" / データ不足={total_insufficient} 件" if total_insufficient > 0 else ""
    )
    if market_mode in ("caution", "full_caution"):
        logger.info(
            f"シグナル検出完了: {len(screened_tickers)} 銘柄 × {len(strategies)} 戦略 → "
            f"watch={watch_count} 件（警戒モードのため新規買い候補は停止）{insufficient_str}"
        )
    else:
        logger.info(
            f"シグナル検出完了: {len(screened_tickers)} 銘柄 × {len(strategies)} 戦略 → "
            f"candidate={candidate_count} 件 / watch={watch_count} 件 / "
            f"通知={len(notify_candidates)} 件{insufficient_str}"
        )
    return notify_candidates


def diagnose_signal_save_count(
    target_date: date,
    market: str,
    expected_count: int,
) -> Optional[str]:
    """daily_signals の保存件数が期待値と一致するか確認する。"""
    if expected_count <= 0:
        return None

    strategies = _enabled_strategies(market)
    expected_total = expected_count * len(strategies)

    with get_connection() as conn:
        row = fetch_one(
            conn,
            "SELECT COUNT(*) AS c FROM daily_signals WHERE date = ? AND market = ?",
            (target_date.strftime("%Y-%m-%d"), market),
        )
    actual_count = int(row["c"]) if row else 0
    if actual_count == expected_total:
        return None
    return (
        f"daily_signals 保存件数不一致（{market} {target_date.isoformat()}）: "
        f"期待={expected_total:,} / 実績={actual_count:,}"
    )


def fetch_near_misses(
    target_date: date,
    market: str = "US",
    limit: int = 5,
) -> list[dict]:
    """監視リスト（watch）のうち RSI 条件まで満たすスイング銘柄を返す。"""
    target_str = target_date.strftime("%Y-%m-%d")
    swing_strategy = get_market_strategy(market)
    with get_connection() as conn:
        rows = fetch_all(
            conn,
            """
            SELECT d.ticker, u.name, d.market, d.close, d.rsi14,
                   d.cond_c_weekly_uptrend, d.cond_c_rsi_oversold, d.cond_c_sar_reversal,
                   d.strategy,
                   t.entry_price, t.stop_loss_price
            FROM daily_signals d
            LEFT JOIN universe_master u ON u.ticker = d.ticker
            LEFT JOIN trade_signal_log t
              ON d.ticker = t.ticker AND d.date = t.signal_date
            WHERE d.date = ? AND d.market = ? AND d.strategy = ?
              AND d.label = 'watch'
              AND d.cond_c_weekly_uptrend = 1
              AND d.cond_c_rsi_oversold = 1
              AND d.cond_c_sar_reversal = 0
            ORDER BY d.rsi14 ASC
            LIMIT ?
            """,
            (target_str, market, swing_strategy, limit),
        )
    result = []
    for r in rows:
        item = dict(r)
        item["pattern"] = PATTERN_SWING
        item["near_miss_type"] = "macd_pending"
        result.append(item)
    return result


fetch_watch_stocks = fetch_near_misses
