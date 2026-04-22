"""
買い候補スコアリングモジュール (v1)

各候補銘柄に対して trend_score / pattern_a_score / pattern_b_score / liquidity_score を計算し、
final_score (0〜100) と recommend_rank / recommend_reason を生成する。

設計方針:
- ハードフィルターは最小限（流動性フィルタ通過銘柄のみに適用）
- 各条件を段階的に点数化し、AND 条件の代わりにスコアで評価
- market_mode は multiplier で final_score に反映（候補消去には使わない）
"""

from typing import Optional

from src.data_store import fetch_all, get_connection
from src.logger import get_logger

logger = get_logger(__name__)

# ============================================================
# 最終スコアの重み
# ============================================================

W_TREND     = 0.35
W_PATTERN_A = 0.30
W_PATTERN_B = 0.20
W_LIQUIDITY = 0.15

# ============================================================
# trend_score サブスコア重み
# ============================================================

_TW_MA200_GROWTH  = 0.35
_TW_MA_ALIGNMENT  = 0.40
_TW_CLOSE_VS_MA25 = 0.25

# ============================================================
# pattern_a_score サブスコア重み
# ============================================================

_PAW_HIGH52W   = 0.50
_PAW_VOL_RATIO = 0.30
_PAW_RSI       = 0.20

# ============================================================
# pattern_b_score サブスコア重み
# ============================================================

_PBW_PULLBACK   = 0.40
_PBW_REBOUND    = 0.35
_PBW_MA25_TREND = 0.25

# ============================================================
# 市場環境 multiplier
# ============================================================

MARKET_MULTIPLIER = {
    "normal":       1.00,
    "caution":      0.88,
    "full_caution": 0.75,
}

# ============================================================
# ランク閾値
# ============================================================

_RANK_THRESHOLDS = [
    (85, "S"),
    (75, "A"),
    (65, "B"),
    (50, "C"),
    (0,  "Watch"),
]

# ============================================================
# 流動性閾値  (threshold, score)
#   JP: 円建て売買代金,  US: ドル建て売買代金
# ============================================================

_JP_TURNOVER_THRESHOLDS = [
    (3_000_000_000, 100),   # 30億円以上
    (1_000_000_000,  80),   # 10億円以上
    (  500_000_000,  60),   # 5億円以上
    (  300_000_000,  30),   # 3億円以上
    (            0,   0),
]

_US_TURNOVER_THRESHOLDS = [
    (200_000_000, 100),     # $200M以上
    (100_000_000,  80),     # $100M以上
    ( 50_000_000,  60),     # $50M以上
    ( 20_000_000,  30),     # $20M以上
    (          0,   0),
]


# ============================================================
# メイン: スコア計算
# ============================================================

def compute_score(
    tk: dict,
    market_mode: str,
    recent_low_5d: Optional[float] = None,
) -> dict:
    """
    銘柄 1 件のスコアを計算して辞書で返す。

    引数:
        tk:             screened_tickers の 1 行（テクニカルデータ含む）
        market_mode:    "normal" / "caution" / "full_caution"
        recent_low_5d:  直近 5 日間の最安値（pattern_b 押し目判定用）

    返り値: {
        "trend_score", "pattern_a_score", "pattern_b_score",
        "liquidity_score", "base_score", "market_multiplier",
        "final_score", "recommend_rank", "recommend_reason",
    }
    """
    t_score   = _calc_trend_score(tk)
    pa_score  = _calc_pattern_a_score(tk)
    pb_score  = _calc_pattern_b_score(tk, recent_low_5d)
    liq_score = _calc_liquidity_score(tk)

    base = (
        W_TREND     * t_score
        + W_PATTERN_A * pa_score
        + W_PATTERN_B * pb_score
        + W_LIQUIDITY * liq_score
    )

    multiplier = MARKET_MULTIPLIER.get(market_mode, 1.0)
    final = max(0, min(100, round(base * multiplier)))

    rank = _recommend_rank(final)

    scores = {
        "trend_score":      t_score,
        "pattern_a_score":  pa_score,
        "pattern_b_score":  pb_score,
        "liquidity_score":  liq_score,
        "base_score":       round(base, 1),
        "market_multiplier": multiplier,
        "final_score":      final,
        "recommend_rank":   rank,
    }
    scores["recommend_reason"] = _build_recommend_reason(tk, scores, market_mode)
    return scores


# ============================================================
# 直近安値の一括取得
# ============================================================

def fetch_recent_lows(
    tickers: list[str],
    target_str: str,
    lookback: int = 5,
) -> dict[str, float]:
    """
    指定銘柄リストの直近 lookback 日間の最安値を一括取得する。

    返り値: {ticker: min_low, ...}
    """
    if not tickers:
        return {}

    placeholders = ",".join(["?"] * len(tickers))
    sql = f"""
        SELECT ticker, MIN(low) AS recent_low
        FROM (
            SELECT ticker, low, date,
                   ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn
            FROM price_history
            WHERE ticker IN ({placeholders})
              AND date <= ?
              AND is_holiday = 0
              AND low IS NOT NULL
        ) sub
        WHERE rn <= ?
        GROUP BY ticker
    """
    params = tuple(tickers) + (target_str, lookback)
    with get_connection() as conn:
        rows = fetch_all(conn, sql, params)

    return {
        r["ticker"]: float(r["recent_low"])
        for r in rows
        if r.get("recent_low") is not None
    }


# ============================================================
# 1. trend_score (0〜100)
# ============================================================

def _calc_trend_score(tk: dict) -> int:
    ma5      = tk.get("ma5")
    ma25     = tk.get("ma25")
    ma75     = tk.get("ma75")
    ma200    = tk.get("ma200")
    ma200_5d = tk.get("ma200_5d_ago")
    close    = tk.get("close") or tk.get("adj_close")

    # 1-1. MA200 上昇トレンド（重み 35%）
    if ma200 and ma200_5d and ma200_5d > 0:
        growth = (ma200 - ma200_5d) / ma200_5d
        if growth >= 0.015:
            ma200_comp = 100
        elif growth >= 0.005:
            ma200_comp = 80
        elif growth >= 0.0:
            ma200_comp = 50
        else:
            ma200_comp = 0
    else:
        ma200_comp = 0

    # 1-2. MA 整列（重み 40%）
    if ma5 and ma25 and ma75 and ma200:
        if ma5 >= ma25 >= ma75 >= ma200:
            align_comp = 100
        elif ma25 >= ma75 >= ma200:
            align_comp = 70
        elif ma75 >= ma200:
            align_comp = 40
        else:
            align_comp = 0
    else:
        align_comp = 0

    # 1-3. 終値 vs MA25（重み 25%）
    if close and ma25 and ma25 > 0:
        ratio = close / ma25
        if ratio >= 1.03:
            close_comp = 100
        elif ratio >= 1.00:
            close_comp = 75
        elif ratio >= 0.98:
            close_comp = 40
        else:
            close_comp = 0
    else:
        close_comp = 0

    score = (
        _TW_MA200_GROWTH  * ma200_comp
        + _TW_MA_ALIGNMENT  * align_comp
        + _TW_CLOSE_VS_MA25 * close_comp
    )
    return max(0, min(100, round(score)))


# ============================================================
# 2. pattern_a_score (0〜100)  — ブレイクアウト評価
# ============================================================

def _calc_pattern_a_score(tk: dict) -> int:
    close    = tk.get("close") or tk.get("adj_close")
    high_52w = tk.get("high_52w")
    volume   = tk.get("volume")
    vol_avg  = tk.get("vol_20d_avg")
    rsi      = tk.get("rsi14")

    # 2-1. 52 週高値近傍（重み 50%）
    if close and high_52w and high_52w > 0:
        ratio_52w = close / high_52w
        if ratio_52w >= 0.98:
            h52_comp = 100
        elif ratio_52w >= 0.95:
            h52_comp = 85
        elif ratio_52w >= 0.92:
            h52_comp = 60
        elif ratio_52w >= 0.90:
            h52_comp = 25
        else:
            h52_comp = 0
    else:
        h52_comp = 0

    # 2-2. 出来高比率（重み 30%）
    if volume and vol_avg and vol_avg > 0:
        vol_ratio = volume / vol_avg
        if vol_ratio >= 2.0:
            vol_comp = 100
        elif vol_ratio >= 1.5:
            vol_comp = 75
        elif vol_ratio >= 1.2:
            vol_comp = 50
        elif vol_ratio >= 1.0:
            vol_comp = 25
        else:
            vol_comp = 0
    else:
        vol_comp = 0

    # 2-3. RSI 適正帯（重み 20%）
    if rsi is not None:
        if 52 <= rsi <= 68:
            rsi_comp = 100
        elif 45 <= rsi <= 72:
            rsi_comp = 70
        elif 40 <= rsi <= 75:
            rsi_comp = 40
        else:
            rsi_comp = 0
    else:
        rsi_comp = 0

    score = (
        _PAW_HIGH52W   * h52_comp
        + _PAW_VOL_RATIO * vol_comp
        + _PAW_RSI       * rsi_comp
    )
    return max(0, min(100, round(score)))


# ============================================================
# 3. pattern_b_score (0〜100)  — 押し目反発評価
# ============================================================

def _calc_pattern_b_score(
    tk: dict,
    recent_low_5d: Optional[float] = None,
) -> int:
    close    = tk.get("close") or tk.get("adj_close")
    open_    = tk.get("open")
    ma25     = tk.get("ma25")
    ma25_5d  = tk.get("ma25_5d_ago")

    # 3-1. 押し目タッチ（重み 40%）
    if recent_low_5d is not None and ma25 and ma25 > 0:
        pullback_ratio = recent_low_5d / ma25
        if pullback_ratio <= 0.98:
            pb_comp = 100
        elif pullback_ratio <= 1.00:
            pb_comp = 75
        elif pullback_ratio <= 1.02:
            pb_comp = 25
        else:
            pb_comp = 0
    else:
        pb_comp = 0

    # 3-2. 反発確認（重み 35%）
    if close and ma25:
        is_bullish = (open_ is not None) and (close > open_)
        if close >= ma25 and is_bullish:
            reb_comp = 100
        elif close >= ma25:
            reb_comp = 70
        elif close >= ma25 * 0.98:
            reb_comp = 30
        else:
            reb_comp = 0
    else:
        reb_comp = 0

    # 3-3. MA25 上昇（重み 25%）
    if ma25 and ma25_5d and ma25_5d > 0:
        growth = (ma25 - ma25_5d) / ma25_5d
        if growth >= 0.01:
            ma25_comp = 100
        elif growth >= 0.0:
            ma25_comp = 70
        elif growth >= -0.002:
            ma25_comp = 30
        else:
            ma25_comp = 0
    else:
        ma25_comp = 0

    score = (
        _PBW_PULLBACK   * pb_comp
        + _PBW_REBOUND    * reb_comp
        + _PBW_MA25_TREND * ma25_comp
    )
    return max(0, min(100, round(score)))


# ============================================================
# 4. liquidity_score (0〜100)
# ============================================================

def _calc_liquidity_score(tk: dict) -> int:
    turnover = tk.get("est_turnover") or 0
    market   = tk.get("market", "JP")

    thresholds = (
        _JP_TURNOVER_THRESHOLDS if market == "JP"
        else _US_TURNOVER_THRESHOLDS
    )
    for threshold_val, score_val in thresholds:
        if turnover >= threshold_val:
            return score_val
    return 0


# ============================================================
# ランク判定
# ============================================================

def _recommend_rank(final_score: int) -> str:
    for threshold, rank in _RANK_THRESHOLDS:
        if final_score >= threshold:
            return rank
    return "Watch"


# ============================================================
# 推薦理由の自動生成
# ============================================================

def _build_recommend_reason(
    tk: dict,
    scores: dict,
    market_mode: str,
) -> str:
    parts: list[str] = []

    trend = scores["trend_score"]
    pa    = scores["pattern_a_score"]
    pb    = scores["pattern_b_score"]
    liq   = scores["liquidity_score"]

    # --- トレンド系 ---
    if trend >= 70:
        ma5  = tk.get("ma5")
        ma25 = tk.get("ma25")
        ma75 = tk.get("ma75")
        ma200 = tk.get("ma200")
        if ma5 and ma25 and ma75 and ma200 and ma5 >= ma25 >= ma75 >= ma200:
            parts.append("短〜長期MA整列が揃っている")
        else:
            parts.append("上昇トレンドが継続中")
    elif trend >= 40:
        parts.append("中期トレンドは良好")

    # --- パターンA 系 ---
    if pa >= 60:
        close    = tk.get("close") or tk.get("adj_close")
        high_52w = tk.get("high_52w")
        volume   = tk.get("volume")
        vol_avg  = tk.get("vol_20d_avg")

        if close and high_52w and high_52w > 0 and close / high_52w >= 0.95:
            parts.append("52週高値に接近")
        if volume and vol_avg and vol_avg > 0 and volume / vol_avg >= 1.5:
            parts.append(f"出来高が平均の{volume / vol_avg:.1f}倍に増加")

    # --- パターンB 系 ---
    if pb >= 60:
        parts.append("押し目反発の形成を確認")
    elif pb >= 40:
        parts.append("押し目買いの兆候あり")

    # --- 流動性 ---
    if liq >= 80:
        parts.append("流動性が高い")

    # --- フォールバック ---
    if not parts:
        if scores["final_score"] >= 50:
            parts.append("複数の条件をある程度充足")
        else:
            parts.append("条件の充足が限定的")

    # 要素は最大3つまで
    main_parts = parts[:3]

    # --- 市場環境の補足（常に付加）---
    if market_mode == "caution":
        main_parts.append("市場環境が警戒モードのため減点あり")
    elif market_mode == "full_caution":
        main_parts.append("市場環境が全面警戒のため大幅減点")

    return "。".join(main_parts)
