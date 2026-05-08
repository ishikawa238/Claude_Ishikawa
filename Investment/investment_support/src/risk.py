"""
リスク指標計算モジュール

VaR (99% 1-day), Beta (60-day), Sharpe Ratio (60-day) を計算する。
numpy を使用。
"""

from typing import Optional

import numpy as np

from src.config import CONFIG
from src.logger import get_logger

logger = get_logger(__name__)

# 無リスク金利（年率、CONFIG から取得可能）
_RISK_FREE = CONFIG.get("risk", {}) if isinstance(CONFIG.get("risk"), dict) else {}
_RF_JP = float(_RISK_FREE.get("risk_free_rate_jp", 0.001))   # 0.1%
_RF_US = float(_RISK_FREE.get("risk_free_rate_us", 0.05))    # 5.0%


def calc_risk_metrics(
    prices: list[float],
    benchmark_prices: list[float],
    market: str = "JP",
) -> dict[str, Optional[float]]:
    """
    リスク指標を計算する。

    引数:
        prices:           対象銘柄の終値リスト（古い順）
        benchmark_prices: ベンチマークの終値リスト（古い順、同日数）
        market:           "JP" or "US"

    返り値: {"var_99_1d": float, "beta_60d": float, "sharpe_60d": float}
             データ不足時は各値が None
    """
    result: dict[str, Optional[float]] = {
        "var_99_1d": None,
        "beta_60d": None,
        "sharpe_60d": None,
    }

    if len(prices) < 10:
        return result

    arr = np.array(prices, dtype=float)
    returns = np.diff(arr) / arr[:-1]

    # VaR (99%, 1-day)
    result["var_99_1d"] = round(float(np.percentile(returns, 1)), 6)

    # Beta & Sharpe — ベンチマーク必要
    if len(benchmark_prices) >= len(prices):
        bm = np.array(benchmark_prices[-len(prices):], dtype=float)
        bm_returns = np.diff(bm) / bm[:-1]

        min_len = min(len(returns), len(bm_returns))
        r = returns[-min_len:]
        bm_r = bm_returns[-min_len:]

        # Beta
        cov = np.cov(r, bm_r)
        bm_var = np.var(bm_r, ddof=1)
        if bm_var > 0:
            result["beta_60d"] = round(float(cov[0, 1] / bm_var), 4)

    # Sharpe Ratio
    rf_daily = (_RF_JP if market == "JP" else _RF_US) / 252
    excess = returns - rf_daily
    std = np.std(excess, ddof=1)
    if std > 0:
        sharpe_annualized = float(np.mean(excess) / std) * np.sqrt(252)
        result["sharpe_60d"] = round(sharpe_annualized, 4)

    return result
