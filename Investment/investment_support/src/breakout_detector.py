"""
新高値ブレイクアウト戦略（モメンタム順張り）の条件ベース検出。

candidate: 60日高値更新 + 出来高が20日平均の N 倍以上
"""

from __future__ import annotations

from typing import Optional

from src.config import get_breakout_config

PATTERN_BREAKOUT = "A"  # DB CHECK 互換（表示: モメンタムブレイクアウト）


def _cfg() -> dict:
    return get_breakout_config()


def evaluate_breakout(
    tk: dict,
    target_str: str,
    *,
    strategy: str,
) -> dict:
    """1銘柄分のブレイクアウト条件を評価する。"""
    ticker = tk["ticker"]
    market = tk["market"]
    close = tk.get("close") or tk.get("adj_close")
    high_prior = tk.get("high_60d_prior")
    vol = tk.get("volume")
    vol_avg = tk.get("vol_20d_avg")
    vol_ratio = (vol / vol_avg) if (vol and vol_avg and vol_avg > 0) else None

    cfg = _cfg()
    vol_min = float(cfg.get("volume_ratio_min", 1.5))
    min_rs = cfg.get("min_rs_score")
    rs_score = tk.get("rs_score")

    c_new_high = _flag(
        close is not None
        and high_prior is not None
        and float(close) > float(high_prior)
    )
    c_volume = _flag(vol_ratio is not None and vol_ratio >= vol_min)
    c_rs = _flag(
        min_rs is None
        or (rs_score is not None and float(rs_score) >= float(min_rs))
    )

    conds = [c_new_high, c_volume, c_rs]
    met = sum(conds)
    total = len(conds)

    if met == total:
        label = "candidate"
    elif c_new_high and c_volume:
        label = "watch"
    else:
        label = "skip"

    return {
        "ticker": ticker,
        "date": target_str,
        "market": market,
        "strategy": strategy,
        "pattern": PATTERN_BREAKOUT if label != "skip" else None,
        "label": label,
        "cond_a_ma200_rising": 0,
        "cond_a_ma_aligned": 0,
        "cond_a_52w_high_near": c_new_high,
        "cond_a_volume_surge": c_volume,
        "cond_a_rsi_ok": c_rs,
        "cond_a_above_ma25": 0,
        "cond_b_ma200_rising": 0,
        "cond_b_ma_aligned": 0,
        "cond_b_pullback_touched": 0,
        "cond_b_bounced": 0,
        "cond_b_ma25_rising": 0,
        "cond_b_rsi_ok": 0,
        "cond_c_weekly_uptrend": 0,
        "cond_c_at_support": 0,
        "cond_c_rsi_oversold": 0,
        "cond_c_sar_reversal": 0,
        "macd_confirm_type": None,
        "close": close,
        "rsi14": tk.get("rsi14"),
        "atr14": tk.get("atr14"),
        "vol_ratio": vol_ratio,
        "rs_score": rs_score,
        "rs_rank": None,
        "_conditions_met_count": met,
        "_conditions_total": total,
        "_conditions_summary": _build_summary(c_new_high, c_volume, c_rs, vol_min),
    }


def _build_summary(new_high: int, volume: int, rs_ok: int, vol_min: float) -> list[str]:
    return [
        "60日高値更新" + (" ✓" if new_high else " ✗"),
        f"出来高≥{vol_min:g}x" + (" ✓" if volume else " ✗"),
        "RS条件" + (" ✓" if rs_ok else " ✗"),
    ]


def _flag(value: bool) -> int:
    return 1 if value else 0
