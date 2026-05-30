"""
買い候補スコアリングモジュール (v1)

各候補銘柄に対して共通指標スコアを計算し、
final_score (0〜100) と Pattern A/B/C の適合度・ラベルを生成する。

設計方針:
- ハードフィルターは最小限（流動性フィルタ通過銘柄のみに適用）
- 各条件を段階的に点数化し、AND 条件の代わりにスコアで評価
- final_score は Pattern A/B/C の加重和ではなく、共通指標の加重平均で評価
- Pattern A/B/C は共通指標から算出する適合度として扱い、勝ち筋のラベル付けに使う
- market_mode は multiplier で final_score に反映（候補消去には使わない）
"""

from typing import Optional

from src.data_store import fetch_all, get_connection
from src.logger import get_logger

logger = get_logger(__name__)

# ============================================================
# 最終スコアの重み（共通指標）
# ============================================================

W_TREND       = 0.24
W_MOMENTUM    = 0.16
W_PULLBACK    = 0.12
W_SUPPORT     = 0.10
W_VOLUME      = 0.10
W_LIQUIDITY   = 0.14
W_RISK        = 0.08
W_FUNDAMENTAL = 0.06

_FINAL_FACTOR_WEIGHTS = {
    "trend": W_TREND,
    "momentum": W_MOMENTUM,
    "pullback": W_PULLBACK,
    "support": W_SUPPORT,
    "volume": W_VOLUME,
    "liquidity": W_LIQUIDITY,
    "risk": W_RISK,
    "fundamental": W_FUNDAMENTAL,
}

# 後方互換: 旧定数名は残すが、final_score の直接材料には使わない。
W_PATTERN_A = W_MOMENTUM
W_PATTERN_B = W_PULLBACK
W_PATTERN_C = W_SUPPORT

_PATTERN_FIT_WEIGHTS = {
    "A": {
        "trend": 0.30,
        "momentum": 0.40,
        "volume": 0.20,
        "risk": 0.10,
    },
    "B": {
        "trend": 0.20,
        "pullback": 0.45,
        "momentum": 0.20,
        "risk": 0.15,
    },
    "C": {
        "trend": 0.30,
        "support": 0.40,
        "risk": 0.15,
        "fundamental": 0.15,
    },
}

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
# pattern_c_score サブスコア重み
# ============================================================

_PCW_WEEKLY_TREND      = 0.20
_PCW_SUPPORT_PROXIMITY = 0.40
_PCW_RSI_DEPTH         = 0.20
_PCW_SAR_CONFIRM       = 0.20

# ============================================================
# 市場環境 multiplier
# ============================================================

MARKET_MULTIPLIER = {
    "normal":       1.00,
    "caution":      0.88,
    "full_caution": 0.75,
}

# ============================================================
# スコア候補昇格閾値
# ============================================================

SCORE_CANDIDATE_MIN_FINAL = 50
SCORE_CANDIDATE_MIN_PATTERN = 60
SCORE_WATCH_MIN_FINAL = 30

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


def get_scoring_definition() -> dict:
    """ダッシュボード表示用に、スコアリング定義を構造化して返す。"""
    return {
        "final_weights": [
            {"key": "trend_score", "label": "トレンド", "weight": W_TREND,
             "summary": "MA200の傾き、MA整列、終値とMA25の位置"},
            {"key": "momentum_score", "label": "モメンタム", "weight": W_MOMENTUM,
             "summary": "52週高値接近、出来高増、RSI適正帯"},
            {"key": "pullback_score", "label": "押し目", "weight": W_PULLBACK,
             "summary": "MA25への押し目、反発、MA25上昇"},
            {"key": "support_score", "label": "サポート", "weight": W_SUPPORT,
             "summary": "週足上昇、サポート近接、売られ過ぎ、SAR反転"},
            {"key": "volume_score", "label": "出来高", "weight": W_VOLUME,
             "summary": "20日平均に対する当日出来高の強さ"},
            {"key": "liquidity_score", "label": "流動性", "weight": W_LIQUIDITY,
             "summary": "推計売買代金による執行しやすさ"},
            {"key": "risk_score", "label": "リスク余地", "weight": W_RISK,
             "summary": "RSI過熱感、短期急騰、ボラティリティを減点"},
            {"key": "fundamental_score", "label": "ファンダメンタル", "weight": W_FUNDAMENTAL,
             "summary": "ROE・利益率・成長率など取得できる財務指標"},
        ],
        "pattern_fit_rules": [
            {"label": "Pattern A", "name": "モメンタムブレイクアウト",
             "weights": _PATTERN_FIT_WEIGHTS["A"],
             "summary": "上昇トレンド、モメンタム、出来高の強さからブレイクアウト適合度を算出"},
            {"label": "Pattern B", "name": "押し目反発",
             "weights": _PATTERN_FIT_WEIGHTS["B"],
             "summary": "押し目形成、反発、トレンド継続性から押し目買い適合度を算出"},
            {"label": "Pattern C", "name": "サポートバウンス",
             "weights": _PATTERN_FIT_WEIGHTS["C"],
             "summary": "週足トレンド、サポート近接、リスク余地からサポート反発適合度を算出"},
        ],
        "subscores": [
            {
                "label": "Trend",
                "items": [
                    {"label": "MA200上昇", "weight": _TW_MA200_GROWTH,
                     "detail": "5営業日前比 +1.5%以上で満点"},
                    {"label": "MA整列", "weight": _TW_MA_ALIGNMENT,
                     "detail": "MA5 >= MA25 >= MA75 >= MA200 で満点"},
                    {"label": "終値 vs MA25", "weight": _TW_CLOSE_VS_MA25,
                     "detail": "終値がMA25を3%以上上回ると満点"},
                ],
            },
            {
                "label": "Pattern A",
                "items": [
                    {"label": "52週高値近傍", "weight": _PAW_HIGH52W,
                     "detail": "終値が52週高値の98%以上で満点"},
                    {"label": "出来高比率", "weight": _PAW_VOL_RATIO,
                     "detail": "20日平均の2.0倍以上で満点"},
                    {"label": "RSI適正帯", "weight": _PAW_RSI,
                     "detail": "RSI 52〜68で満点"},
                ],
            },
            {
                "label": "Pattern B",
                "items": [
                    {"label": "押し目タッチ", "weight": _PBW_PULLBACK,
                     "detail": "直近安値がMA25の98%以下で満点"},
                    {"label": "反発確認", "weight": _PBW_REBOUND,
                     "detail": "終値がMA25以上かつ陽線で満点"},
                    {"label": "MA25上昇", "weight": _PBW_MA25_TREND,
                     "detail": "5営業日前比 +1%以上で満点"},
                ],
            },
            {
                "label": "Pattern C",
                "items": [
                    {"label": "週足MA25上昇", "weight": _PCW_WEEKLY_TREND,
                     "detail": "週足MA25が5週前より上なら満点"},
                    {"label": "サポート近接", "weight": _PCW_SUPPORT_PROXIMITY,
                     "detail": "終値とサポートの距離が1%以内で満点"},
                    {"label": "RSI売られ過ぎ", "weight": _PCW_RSI_DEPTH,
                     "detail": "RSI 20以下で満点"},
                    {"label": "SAR反転", "weight": _PCW_SAR_CONFIRM,
                     "detail": "パラボリックSARが上昇方向なら満点"},
                ],
            },
        ],
        "rank_thresholds": [
            {"rank": rank, "min_score": threshold}
            for threshold, rank in _RANK_THRESHOLDS
        ],
        "market_multipliers": [
            {"mode": "normal", "label": "通常", "multiplier": MARKET_MULTIPLIER["normal"],
             "detail": "新規買い候補を発信する"},
            {"mode": "caution", "label": "警戒", "multiplier": MARKET_MULTIPLIER["caution"],
             "detail": "スコアは計算するが新規買い候補は停止"},
            {"mode": "full_caution", "label": "全面警戒", "multiplier": MARKET_MULTIPLIER["full_caution"],
             "detail": "大きく減点し、新規買い候補は停止"},
        ],
        "candidate_rules": [
            {"label": "全条件一致候補",
             "detail": "A/B/Cいずれかの厳密条件をすべて満たすと candidate"},
            {"label": "スコア候補",
             "detail": f"final_score >= {SCORE_CANDIDATE_MIN_FINAL} かつ主要パターン適合度 >= {SCORE_CANDIDATE_MIN_PATTERN}"},
            {"label": "Watch",
             "detail": f"candidate未満でも final_score >= {SCORE_WATCH_MIN_FINAL} は監視銘柄として保存"},
            {"label": "攻めモード",
             "detail": "スコア候補化後に strategy.offensive.min_final_score / min_rank でさらに絞り込み"},
        ],
        "liquidity_thresholds": {
            "JP": [{"threshold": threshold, "score": score}
                   for threshold, score in _JP_TURNOVER_THRESHOLDS[:-1]],
            "US": [{"threshold": threshold, "score": score}
                   for threshold, score in _US_TURNOVER_THRESHOLDS[:-1]],
        },
    }


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
        "factor_scores", "pattern_fit", "primary_pattern",
        "trend_score", "pattern_a_score", "pattern_b_score",
        "liquidity_score", "base_score", "market_multiplier",
        "final_score", "recommend_rank", "recommend_reason",
    }
    """
    t_score   = _calc_trend_score(tk)
    momentum_score = _calc_pattern_a_score(tk)
    pullback_score = _calc_pattern_b_score(tk, recent_low_5d)
    support_score  = _calc_pattern_c_score(tk)
    liq_score = _calc_liquidity_score(tk)
    volume_score = _calc_volume_score(tk)
    risk_score = _calc_risk_score(tk)
    fundamental_score = _calc_fundamental_score(tk)

    factor_scores = {
        "trend": t_score,
        "momentum": momentum_score,
        "pullback": pullback_score,
        "support": support_score,
        "volume": volume_score,
        "liquidity": liq_score,
        "risk": risk_score,
        "fundamental": fundamental_score,
    }
    pattern_fit = _calc_pattern_fit(factor_scores)
    base = _weighted_average(factor_scores, _FINAL_FACTOR_WEIGHTS)
    multiplier = MARKET_MULTIPLIER.get(market_mode, 1.0)
    final = max(0, min(100, round(base * multiplier)))

    rank = _recommend_rank(final)
    primary_pattern = _primary_pattern(pattern_fit)

    scores = {
        "trend_score":      t_score,
        "pattern_a_score":  pattern_fit["A"],
        "pattern_b_score":  pattern_fit["B"],
        "pattern_c_score":  pattern_fit["C"],
        "liquidity_score":  liq_score,
        "base_score":       round(base, 1),
        "market_multiplier": multiplier,
        "final_score":      final,
        "recommend_rank":   rank,
        "factor_scores":    factor_scores,
        "pattern_fit":      pattern_fit,
        "primary_pattern":  primary_pattern,
        "secondary_patterns": _secondary_patterns(pattern_fit, primary_pattern),
        "raw_pattern_scores": {
            "A": momentum_score,
            "B": pullback_score,
            "C": support_score,
        },
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
        if r["recent_low"] is not None
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
# 4. pattern_c_score (0〜100)  — サポートバウンス評価
# ============================================================

def _calc_pattern_c_score(tk: dict) -> int:
    close         = tk.get("close") or tk.get("adj_close")
    weekly_ma25   = tk.get("weekly_ma25")
    weekly_5w_ago = tk.get("weekly_ma25_5w_ago")
    support_level = tk.get("support_level")
    rsi           = tk.get("rsi14")
    sar_direction = tk.get("sar_direction")

    # 4-1. 週足MA25上昇（重み 20%）
    if weekly_ma25 and weekly_5w_ago and weekly_5w_ago > 0:
        weekly_comp = 100 if weekly_ma25 > weekly_5w_ago else 0
    else:
        weekly_comp = 0

    # 4-2. サポート近接度（重み 40%）
    if close and support_level and support_level > 0:
        distance_pct = abs(close - support_level) / support_level * 100
        if distance_pct <= 1.0:
            sup_comp = 100
        elif distance_pct <= 3.0:
            sup_comp = 75
        elif distance_pct <= 5.0:
            sup_comp = 40
        elif distance_pct <= 8.0:
            sup_comp = 15
        else:
            sup_comp = 0
    else:
        sup_comp = 0

    # 4-3. RSI 売られ過ぎ深さ（重み 20%）
    if rsi is not None:
        if rsi <= 20:
            rsi_comp = 100
        elif rsi <= 25:
            rsi_comp = 85
        elif rsi <= 30:
            rsi_comp = 70
        elif rsi <= 35:
            rsi_comp = 40
        else:
            rsi_comp = 0
    else:
        rsi_comp = 0

    # 4-4. パラボリックSAR 反転確認（重み 20%）
    if sar_direction == "up":
        sar_comp = 100
    else:
        sar_comp = 0

    score = (
        _PCW_WEEKLY_TREND      * weekly_comp
        + _PCW_SUPPORT_PROXIMITY * sup_comp
        + _PCW_RSI_DEPTH       * rsi_comp
        + _PCW_SAR_CONFIRM     * sar_comp
    )
    if weekly_comp == 0:
        score = min(score, 59)
    return max(0, min(100, round(score)))


# ============================================================
# 5. liquidity_score (0〜100)
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
# 共通指標スコア
# ============================================================

def _calc_volume_score(tk: dict) -> int:
    volume = tk.get("volume")
    vol_avg = tk.get("vol_20d_avg")
    if volume and vol_avg and vol_avg > 0:
        ratio = volume / vol_avg
        if ratio >= 2.0:
            return 100
        if ratio >= 1.5:
            return 75
        if ratio >= 1.2:
            return 55
        if ratio >= 1.0:
            return 35
    return 0


def _calc_risk_score(tk: dict) -> int:
    """高いほどエントリー余地がある。過熱・急騰・高ボラは減点する。"""
    components: list[int] = []

    rsi = tk.get("rsi14")
    if rsi is not None:
        if 45 <= rsi <= 68:
            components.append(100)
        elif 35 <= rsi <= 75:
            components.append(70)
        elif rsi <= 80:
            components.append(40)
        else:
            components.append(15)

    volatility = tk.get("volatility_20d")
    if volatility is not None:
        if volatility <= 0.02:
            components.append(100)
        elif volatility <= 0.04:
            components.append(75)
        elif volatility <= 0.06:
            components.append(45)
        else:
            components.append(20)

    ret_5d = tk.get("return_5d")
    if ret_5d is not None:
        if ret_5d >= 0.30:
            components.append(15)
        elif ret_5d >= 0.20:
            components.append(35)
        elif ret_5d >= 0.10:
            components.append(65)
        else:
            components.append(100)

    if not components:
        return 50
    return max(0, min(100, round(sum(components) / len(components))))


def _calc_fundamental_score(tk: dict) -> int:
    """取得できる財務指標だけで評価し、欠損時は中立点を返す。"""
    components: list[int] = []

    roe = tk.get("roe") or tk.get("roe_ttm")
    if roe is not None:
        if roe >= 0.20:
            components.append(100)
        elif roe >= 0.12:
            components.append(80)
        elif roe >= 0.08:
            components.append(60)
        elif roe >= 0.0:
            components.append(35)
        else:
            components.append(0)

    margin = tk.get("operating_margin") or tk.get("profit_margin")
    if margin is not None:
        if margin >= 0.20:
            components.append(100)
        elif margin >= 0.12:
            components.append(80)
        elif margin >= 0.06:
            components.append(55)
        elif margin >= 0.0:
            components.append(30)
        else:
            components.append(0)

    growth = tk.get("sales_growth") or tk.get("revenue_growth") or tk.get("eps_growth")
    if growth is not None:
        if growth >= 0.20:
            components.append(100)
        elif growth >= 0.10:
            components.append(80)
        elif growth >= 0.03:
            components.append(55)
        elif growth >= 0.0:
            components.append(35)
        else:
            components.append(10)

    if not components:
        return 50
    return max(0, min(100, round(sum(components) / len(components))))


def _calc_pattern_fit(factor_scores: dict[str, int]) -> dict[str, int]:
    return {
        pattern: _weighted_score(factor_scores, weights)
        for pattern, weights in _PATTERN_FIT_WEIGHTS.items()
    }


def _weighted_score(scores: dict[str, int], weights: dict[str, float]) -> int:
    return max(0, min(100, round(_weighted_average(scores, weights))))


def _weighted_average(scores: dict[str, int], weights: dict[str, float]) -> float:
    total = sum((scores.get(key) or 0) * weight for key, weight in weights.items())
    return max(0.0, min(100.0, total))


def _primary_pattern(pattern_fit: dict[str, int]) -> str:
    pattern, _score = max(pattern_fit.items(), key=lambda item: item[1])
    return pattern


def _secondary_patterns(
    pattern_fit: dict[str, int],
    primary_pattern: str,
) -> list[str]:
    return [
        pattern
        for pattern, score in pattern_fit.items()
        if pattern != primary_pattern and score >= SCORE_CANDIDATE_MIN_PATTERN
    ]


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
    pc    = scores["pattern_c_score"]
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

    # --- パターンC 系 ---
    if pc >= 60:
        parts.append("サポートレベルからの反発を確認")
    elif pc >= 40:
        parts.append("サポートバウンスの兆候あり")

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
