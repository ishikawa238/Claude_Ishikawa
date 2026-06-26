"""
EMA押し目スイング戦略の条件ベース検出。

watch:  EMA短期 > EMA長期（上昇トレンド）
candidate: watch + RSI売られ過ぎ + MACD確認
"""

from datetime import date
from typing import Optional

from src.config import CONFIG, get_swing_config
from src.data_store import fetch_all, get_connection

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


def evaluate_swing(tk: dict, target_str: str) -> dict:
    """
    1銘柄分のスイング条件を評価する。

    daily_signals 保存用の辞書を返す（cond_c_* 列をスイング条件として再利用）。
    """
    ticker = tk["ticker"]
    market = tk["market"]
    close = tk.get("close") or tk.get("adj_close")
    ema_short = tk.get("ema25")
    ema_long = tk.get("ema75")
    rsi = tk.get("rsi14")
    vol = tk.get("volume")
    vol_avg = tk.get("vol_20d_avg")
    vol_ratio = (vol / vol_avg) if (vol and vol_avg and vol_avg > 0) else None

    c_uptrend = _flag(ema_short and ema_long and ema_short > ema_long)
    c_rsi = _flag(_rsi_condition_met(ticker, target_str, rsi))
    c_macd = _flag(_macd_confirm(tk))

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
        "pattern": PATTERN_SWING if label != "skip" else None,
        "label": label,
        "cond_c_weekly_uptrend": c_uptrend,
        "cond_c_at_support": 0,
        "cond_c_rsi_oversold": c_rsi,
        "cond_c_sar_reversal": c_macd,
        "close": close,
        "rsi14": rsi,
        "atr14": tk.get("atr14"),
        "vol_ratio": vol_ratio,
        "rs_rank": None,
        "name": tk.get("name"),
        "support_level": tk.get("support_level"),
        "ema25": ema_short,
        "ema75": ema_long,
        "macd_diff": tk.get("macd_diff"),
        "macd_dea": tk.get("macd_dea"),
        "macd_hist": tk.get("macd_hist"),
        "_conditions_met_count": met,
        "_conditions_total": total,
        "_conditions_summary": _build_conditions_summary(c_uptrend, c_rsi, c_macd),
    }


def _rsi_condition_met(ticker: str, target_str: str, rsi_today: Optional[float]) -> bool:
    if rsi_today is not None and rsi_today <= _RSI_MAX:
        return True
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
    return any(float(r["rsi14"]) <= _RSI_MAX for r in rows)


def _macd_confirm(tk: dict) -> bool:
    diff = tk.get("macd_diff")
    dea = tk.get("macd_dea")
    hist = tk.get("macd_hist")
    diff_prev = tk.get("macd_diff_prev")
    dea_prev = tk.get("macd_dea_prev")
    hist_prev = tk.get("macd_hist_prev")

    if diff is None or dea is None or hist is None:
        return False

    # ヒストグラム反転（マイナス圏から上向き）
    if hist_prev is not None and hist_prev <= 0 and hist > hist_prev:
        return True

    # ゴールデンクロス: DIFF が DEA を下から上へ
    if (
        diff_prev is not None
        and dea_prev is not None
        and diff_prev <= dea_prev
        and diff > dea
    ):
        return True

    return False


def _build_conditions_summary(uptrend: int, rsi: int, macd: int) -> list[str]:
    items = [
        f"EMA{_EMA_SHORT}>EMA{_EMA_LONG}" + (" ✓" if uptrend else " ✗"),
        f"RSI≤{_RSI_MAX:g}" + (" ✓" if rsi else " ✗"),
        "MACD確認" + (" ✓" if macd else " ✗"),
    ]
    return items


def _flag(value: bool) -> int:
    return 1 if value else 0
