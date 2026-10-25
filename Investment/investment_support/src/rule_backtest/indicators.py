"""
バックテスト用のテクニカル指標（銘柄ごとの時系列）。

technical_calculator.calculate_snapshot を日ごとに呼ぶと「日数 × 読み込み日数」の計算量になるため、
同じ式を銘柄ごとに時系列で1回だけ計算する。i 番目の値は i 日目までの価格だけを使い、
calculate_snapshot(ticker, dates[i], prices[:i + 1]) と一致する。
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np

from src.technical_calculator import (
    _SUPPORT_LOOKBACK,
    _detect_support_resistance,
    _ema_series,
)

_MACD_FAST = 12
_MACD_SLOW = 26
_MACD_SIGNAL = 9
_ACTIVE_WINDOW_DAYS = 50
_RETURN_LOOKBACK = 126

# 前日比がこの範囲を外れたら分割・併合など価格の不連続とみなす（docs/signal_expectancy_analysis.md）
PRICE_JUMP_LOW = 0.4
PRICE_JUMP_HIGH = 2.5


def compute_indicators(
    dates: list[str],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float],
) -> dict[str, np.ndarray]:
    """シグナル判定と出口判定に使う指標をまとめて返す（値が無い日は NaN）。"""
    n = len(closes)
    return {
        "ema25": _to_array(_ema_series(closes, 25), n),
        "ema75": _to_array(_ema_series(closes, 75), n),
        **_macd_series(closes),
        "rsi14": np.array(_rsi_series(closes, 14)),
        "atr14": np.array(_atr_series(closes, highs, lows, 14)),
        "ma25": np.array(_sma_series(closes, 25)),
        "vol_20d_avg": np.array(_sma_series(volumes, 20)),
        "high_60d_prior": np.array(_prior_max_series(closes, 60)),
        "active_days": _active_days_series(dates, volumes, _ACTIVE_WINDOW_DAYS),
    }


def return_series(closes: list[float], period: int = _RETURN_LOOKBACK) -> np.ndarray:
    """N 日騰落率（%）。technical_calculator._return_pct と同じ式。"""
    out = np.full(len(closes), np.nan)
    for i in range(period, len(closes)):
        prev = closes[i - period]
        if prev != 0:
            out[i] = (closes[i] - prev) / prev * 100
    return out


def price_jump_flags(closes: list[float]) -> np.ndarray:
    """前日終値からの変化が不連続（分割未調整など）な日を True にする。"""
    flags = np.zeros(len(closes), dtype=bool)
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        if prev > 0:
            ratio = closes[i] / prev
            flags[i] = ratio < PRICE_JUMP_LOW or ratio > PRICE_JUMP_HIGH
    return flags


def support_resistance_at(closes, highs, lows, i: int) -> tuple[float | None, float | None]:
    """i 日目時点のサポート/レジスタンス（必要な日だけ計算する）。引数は list / ndarray。"""
    start = max(0, i + 1 - _SUPPORT_LOOKBACK)

    def window(values) -> list[float]:
        return np.asarray(values[start:i + 1], dtype=float).tolist()

    sup, res = _detect_support_resistance(
        window(closes), window(highs), window(lows), _SUPPORT_LOOKBACK,
    )
    return (
        float(sup) if sup is not None else None,
        float(res) if res is not None else None,
    )


# ============================================================
# 時系列計算ヘルパー
# ============================================================

def _to_array(values: list, n: int) -> np.ndarray:
    out = np.full(n, np.nan)
    for i, v in enumerate(values):
        if v is not None:
            out[i] = v
    return out


def _sma_series(values: list[float], period: int) -> list[float]:
    out = [np.nan] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = sum(values[i - period + 1:i + 1]) / period
    return out


def _prior_max_series(closes: list[float], period: int) -> list[float]:
    """当日を含まない直近 period 日の最高終値（high_60d_prior）。"""
    out = [np.nan] * len(closes)
    for i in range(period, len(closes)):
        out[i] = max(closes[i - period:i])
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _rsi_series(closes: list[float], period: int) -> list[float]:
    """Wilder 平滑化 RSI。technical_calculator._rsi と同じ初期化・平滑化。"""
    n = len(closes)
    out = [np.nan] * n
    if n < period + 1:
        return out
    gains = []
    losses = []
    for i in range(1, n):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for j in range(period, n - 1):
        avg_gain = (avg_gain * (period - 1) + gains[j]) / period
        avg_loss = (avg_loss * (period - 1) + losses[j]) / period
        out[j + 1] = _rsi_value(avg_gain, avg_loss)
    return out


def _atr_series(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    period: int,
) -> list[float]:
    """Wilder 平滑化 ATR。technical_calculator._atr と同じ初期化・平滑化。"""
    n = len(closes)
    out = [np.nan] * n
    if n < period + 1:
        return out
    trs = []
    for i in range(1, n):
        h, low, prev_c = highs[i], lows[i], closes[i - 1]
        trs.append(max(h - low, abs(h - prev_c), abs(low - prev_c)))
    atr = sum(trs[:period]) / period
    out[period] = atr
    for j in range(period, n - 1):
        atr = (atr * (period - 1) + trs[j]) / period
        out[j + 1] = atr
    return out


def _macd_series(closes: list[float]) -> dict[str, np.ndarray]:
    """technical_calculator._macd_fields の当日値・前日値を時系列で返す。"""
    n = len(closes)
    keys = (
        "macd_diff", "macd_dea", "macd_hist",
        "macd_diff_prev", "macd_dea_prev", "macd_hist_prev",
    )
    out = {k: np.full(n, np.nan) for k in keys}
    if n < _MACD_SLOW + _MACD_SIGNAL:
        return out

    fast = _ema_series(closes, _MACD_FAST)
    slow = _ema_series(closes, _MACD_SLOW)
    diff = [
        f - s if f is not None and s is not None else None
        for f, s in zip(fast, slow)
    ]
    first_valid = _MACD_SLOW - 1
    dea = _ema_series(diff[first_valid:], _MACD_SIGNAL)

    for i in range(_MACD_SLOW + _MACD_SIGNAL - 1, n):
        d, e = diff[i], dea[i - first_valid]
        d_prev, e_prev = diff[i - 1], dea[i - 1 - first_valid]
        out["macd_diff"][i] = d
        out["macd_dea"][i] = e
        out["macd_hist"][i] = d - e
        if d_prev is not None and e_prev is not None:
            out["macd_diff_prev"][i] = d_prev
            out["macd_dea_prev"][i] = e_prev
            out["macd_hist_prev"][i] = d_prev - e_prev
    return out


def _active_days_series(dates: list[str], volumes: list[float], window_days: int) -> np.ndarray:
    """各日の「直近 window_days 暦日で出来高 > 0 の日数」（market_filter._filter_inactive と同じ数え方）。"""
    n = len(dates)
    out = np.zeros(n, dtype=np.int32)
    days = [date.fromisoformat(d) for d in dates]
    left = 0
    count = 0
    for i in range(n):
        if volumes[i] > 0:
            count += 1
        lower = days[i] - timedelta(days=window_days)
        while days[left] < lower:
            if volumes[left] > 0:
                count -= 1
            left += 1
        out[i] = count
    return out
