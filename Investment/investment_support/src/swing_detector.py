"""
EMA押し目スイング戦略の条件ベース検出。

watch:  EMA短期 > EMA長期（上昇トレンド）
candidate: watch + RSI売られ過ぎ + MACD確認
"""

from datetime import date
from typing import Optional

from src.config import CONFIG, get_swing_config
from src.data_store import fetch_all, get_connection
from src.rs_score import dynamic_rsi_threshold

PATTERN_SWING = "C"  # DB CHECK 制約互換（表示名はスイング）

_SWING = get_swing_config()
_EMA_SHORT = int(_SWING.get("ema_short", 25))
_EMA_LONG = int(_SWING.get("ema_long", 75))
_RSI_MAX = float(_SWING.get("rsi_max", 35))
_RSI_LOOKBACK = int(_SWING.get("rsi_lookback_days", 5))
_MACD_FAST = int(_SWING.get("macd_fast", 12))
_MACD_SLOW = int(_SWING.get("macd_slow", 26))
_MACD_SIGNAL = int(_SWING.get("macd_signal", 9))


def reload_swing_params() -> None:
    """CONFIG 再読み込み後にパラメータを更新する。"""
    global _SWING, _EMA_SHORT, _EMA_LONG, _RSI_MAX, _RSI_LOOKBACK
    global _MACD_FAST, _MACD_SLOW, _MACD_SIGNAL
    _SWING = get_swing_config()
    _EMA_SHORT = int(_SWING.get("ema_short", 25))
    _EMA_LONG = int(_SWING.get("ema_long", 75))
    _RSI_MAX = float(_SWING.get("rsi_max", 35))
    _RSI_LOOKBACK = int(_SWING.get("rsi_lookback_days", 5))
    _MACD_FAST = int(_SWING.get("macd_fast", 12))
    _MACD_SLOW = int(_SWING.get("macd_slow", 26))
    _MACD_SIGNAL = int(_SWING.get("macd_signal", 9))


def evaluate_swing(
    tk: dict,
    target_str: str,
    *,
    rsi_history: list[float] | None = None,
    strategy: str | None = None,
) -> dict:
    """
    1銘柄分のスイング条件を評価する。

    daily_signals 保存用の辞書を返す（cond_c_* 列をスイング条件として再利用）。
    """
    ticker = tk["ticker"]
    market = tk["market"]
    if strategy is None:
        from src.config import get_market_strategy
        strategy = get_market_strategy(market)
    close = tk.get("close") or tk.get("adj_close")
    ema_short = tk.get("ema25")
    ema_long = tk.get("ema75")
    rsi = tk.get("rsi14")
    vol = tk.get("volume")
    vol_avg = tk.get("vol_20d_avg")
    vol_ratio = (vol / vol_avg) if (vol and vol_avg and vol_avg > 0) else None
    rs_score = tk.get("rs_score")

    rsi_max = dynamic_rsi_threshold(rs_score, _RSI_MAX)
    c_uptrend = _flag(ema_short and ema_long and ema_short > ema_long)
    c_rsi = _flag(_rsi_condition_met(
        ticker, target_str, rsi, rsi_history=rsi_history, rsi_max=rsi_max,
    ))
    macd_detail = macd_confirm_detail(tk)
    c_macd = _flag(macd_detail is not None)

    conds = [c_uptrend, c_rsi, c_macd]
    met = sum(conds)
    total = len(conds)

    if met == total:
        label = "candidate"
    elif c_uptrend and met >= 1:
        label = "watch"
    elif c_uptrend:
        label = "watch"
    else:
        label = "skip"

    return {
        "ticker": ticker,
        "date": target_str,
        "market": market,
        "strategy": strategy,
        "pattern": PATTERN_SWING if label != "skip" else None,
        "label": label,
        "cond_c_weekly_uptrend": c_uptrend,
        "cond_c_at_support": 0,
        "cond_c_rsi_oversold": c_rsi,
        "cond_c_sar_reversal": c_macd,
        "macd_confirm_type": macd_detail,
        "close": close,
        "rsi14": rsi,
        "atr14": tk.get("atr14"),
        "vol_ratio": vol_ratio,
        "rs_score": rs_score,
        "rs_rank": None,
        "_rsi_threshold": rsi_max,
        "name": tk.get("name"),
        "support_level": tk.get("support_level"),
        "ema25": ema_short,
        "ema75": ema_long,
        "macd_diff": tk.get("macd_diff"),
        "macd_dea": tk.get("macd_dea"),
        "macd_hist": tk.get("macd_hist"),
        "_conditions_met_count": met,
        "_conditions_total": total,
        "_conditions_summary": _build_conditions_summary(
            c_uptrend, c_rsi, macd_detail, rsi_max=rsi_max,
        ),
    }


def _rsi_condition_met(
    ticker: str,
    target_str: str,
    rsi_today: Optional[float],
    *,
    rsi_history: list[float] | None = None,
    rsi_max: float | None = None,
) -> bool:
    threshold = rsi_max if rsi_max is not None else _RSI_MAX
    if rsi_today is not None and rsi_today <= threshold:
        return True
    if rsi_history:
        return any(v <= threshold for v in rsi_history)
    if _RSI_LOOKBACK <= 0:
        return False
    try:
        with get_connection() as conn:
            rows = fetch_all(
                conn,
                "SELECT rsi14 FROM technical_snapshot "
                "WHERE ticker = ? AND date <= ? AND rsi14 IS NOT NULL "
                "ORDER BY date DESC LIMIT ?",
                (ticker, target_str, _RSI_LOOKBACK),
            )
    except Exception:
        return False
    return any(float(r["rsi14"]) <= threshold for r in rows)


def macd_histogram_reversal(tk: dict) -> bool:
    """ヒストグラムがマイナス圏で前日より上向き。"""
    hist = tk.get("macd_hist")
    hist_prev = tk.get("macd_hist_prev")
    if hist is None or hist_prev is None:
        return False
    return hist_prev <= 0 and hist > hist_prev


def macd_golden_cross(tk: dict) -> bool:
    """DIF（MACD線）が DEA（シグナル線）を下から上へクロス。"""
    diff = tk.get("macd_diff")
    dea = tk.get("macd_dea")
    diff_prev = tk.get("macd_diff_prev")
    dea_prev = tk.get("macd_dea_prev")
    if None in (diff, dea, diff_prev, dea_prev):
        return False
    return diff_prev <= dea_prev and diff > dea


def macd_confirm_detail(tk: dict) -> Optional[str]:
    """MACD確認の内訳: None / 'hist' / 'gc' / 'both'。"""
    hist_ok = macd_histogram_reversal(tk)
    gc_ok = macd_golden_cross(tk)
    if hist_ok and gc_ok:
        return "both"
    if hist_ok:
        return "hist"
    if gc_ok:
        return "gc"
    return None


def macd_confirm_flags(row: dict) -> tuple[bool, bool]:
    """(ヒスト反転, GC) の充足フラグ。macd_confirm_type があれば優先。"""
    ctype = row.get("macd_confirm_type")
    if ctype == "hist":
        return True, False
    if ctype == "gc":
        return False, True
    if ctype == "both":
        return True, True
    tk = {
        "macd_diff": row.get("macd_diff"),
        "macd_dea": row.get("macd_dea"),
        "macd_hist": row.get("macd_hist"),
        "macd_diff_prev": row.get("macd_diff_prev"),
        "macd_dea_prev": row.get("macd_dea_prev"),
        "macd_hist_prev": row.get("macd_hist_prev"),
    }
    return macd_histogram_reversal(tk), macd_golden_cross(tk)


def macd_confirm_label(detail: Optional[str]) -> str:
    """表示用ラベル。"""
    if detail == "both":
        return "ヒスト+GC"
    if detail == "hist":
        return "ヒスト反転"
    if detail == "gc":
        return "GC"
    return "未達"


def _macd_confirm(tk: dict) -> bool:
    return macd_confirm_detail(tk) is not None


def _build_conditions_summary(
    uptrend: int, rsi: int, macd_detail: Optional[str],
    *, rsi_max: float | None = None,
) -> list[str]:
    rsi_label = f"RSI≤{(rsi_max if rsi_max is not None else _RSI_MAX):g}"
    if macd_detail == "both":
        macd_lbl = "MACD:ヒスト+GC ✓"
    elif macd_detail == "hist":
        macd_lbl = "MACD:ヒスト ✓"
    elif macd_detail == "gc":
        macd_lbl = "MACD:GC ✓"
    else:
        macd_lbl = "MACD ✗"
    items = [
        f"EMA{_EMA_SHORT}>EMA{_EMA_LONG}" + (" ✓" if uptrend else " ✗"),
        rsi_label + (" ✓" if rsi else " ✗"),
        macd_lbl,
    ]
    return items


def _flag(value: bool) -> int:
    return 1 if value else 0


def evaluate_watchlist_swing(
    tk: dict,
    target_str: str,
    *,
    macd_require_both: bool = True,
    rsi_history: list[float] | None = None,
) -> dict | None:
    """
    お気に入り監視用: 3条件すべて充足時のみ dict を返す。
    macd_require_both=True のとき MACD はヒスト反転+GC 両方必須。
    """
    row = evaluate_swing(tk, target_str, rsi_history=rsi_history)
    if not row.get("cond_c_weekly_uptrend"):
        return None
    if not row.get("cond_c_rsi_oversold"):
        return None
    macd_type = row.get("macd_confirm_type")
    if macd_require_both:
        if macd_type != "both":
            return None
    elif not macd_type:
        return None
    row["label"] = "candidate"
    return row
