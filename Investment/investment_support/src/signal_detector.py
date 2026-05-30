"""
売買シグナル検出モジュール

パターンA: モメンタムブレイクアウト
パターンB: 押し目買い
パターンC: サポートバウンス（週足上昇 + サポート近傍 + RSI売られ過ぎ + SAR反転）

各条件の充足状況をフラグで記録し、daily_signals テーブルに保存する。
市場環境モードにかかわらずシグナル検出を実施する。
market_mode は entry_calculator を通じて trade_signal_log に記録される。
"""

from datetime import date
from typing import Optional

from src.config import CONFIG, get_offensive_config, is_offensive
from src.data_store import fetch_all, get_connection, upsert
from src.logger import get_logger
from src.scoring import (
    SCORE_CANDIDATE_MIN_FINAL,
    SCORE_CANDIDATE_MIN_PATTERN,
    SCORE_WATCH_MIN_FINAL,
    compute_score,
    fetch_recent_lows,
)

logger = get_logger(__name__)

_SIG = CONFIG["signal"]
_PA_RSI_MIN       = float(_SIG["pattern_a_rsi_min"])
_PA_RSI_MAX       = float(_SIG["pattern_a_rsi_max"])
_PA_VOL_RATIO     = float(_SIG["pattern_a_volume_ratio_min"])
_PA_52W_PCT       = float(_SIG["pattern_a_52w_high_pct"])
_PB_RSI_MIN       = float(_SIG["pattern_b_rsi_min"])
_PB_RSI_MAX       = float(_SIG["pattern_b_rsi_max"])
_PB_MA25_TOUCH    = float(_SIG["pattern_b_ma25_touch_pct"])
_PB_LOOKBACK      = int(_SIG["pattern_b_lookback_days"])
_PB_BOUNCE_DAYS   = int(_SIG.get("pattern_b_bounce_confirm_days", 1))
_PC_RSI_MAX       = float(_SIG.get("pattern_c_rsi_max", 35))
_PC_SUPPORT_TOL   = float(_SIG.get("pattern_c_support_tolerance_pct", 3.0))
_MAX_CANDIDATES   = int(_SIG["max_candidates_per_pattern"])


# ============================================================
# メイン: シグナル検出と保存
# ============================================================

def detect_and_save_signals(
    screened_tickers: list[dict],
    target_date: date,
    market_mode: str,
) -> list[dict]:
    """
    流動性フィルタ通過銘柄に対してシグナル検出を行い、daily_signals に保存する。

    引数:
        screened_tickers: market_filter.get_screened_tickers() の返り値
        target_date: 対象日
        market_mode: "normal" / "caution" / "full_caution"

    返り値: label='candidate' の銘柄リスト（エントリーセット計算に使用）
    """
    target_str = target_date.strftime("%Y-%m-%d")
    candidates: list[dict] = []
    all_rows: list[dict] = []
    data_insufficient_count = 0

    logger.info(f"市場環境 [{market_mode}]: シグナル検出を実施")

    # 直近5日の最安値を一括取得（pattern_b スコア用）
    tickers_list = [tk["ticker"] for tk in screened_tickers]
    recent_lows = fetch_recent_lows(tickers_list, target_str) if tickers_list else {}

    for tk in screened_tickers:
        ticker = tk["ticker"]
        market = tk["market"]

        # データ不足チェック（主要テクニカル指標の欠損）
        _key_fields = ("ma200", "ma25", "rsi14")
        if any(tk.get(f) is None for f in _key_fields):
            data_insufficient_count += 1

        # スコア計算
        recent_low = recent_lows.get(ticker)
        scores = compute_score(tk, market_mode, recent_low)

        # パターンA評価（既存 cond_* フラグ維持）
        row_a = _eval_pattern_a(tk, target_str, market)
        # パターンB評価
        row_b = _eval_pattern_b(tk, target_str, market)
        # パターンC評価（サポートバウンス）
        row_c = _eval_pattern_c(tk, target_str, market)
        pattern_rows = {"A": row_a, "B": row_b, "C": row_c}

        # 最も強いシグナルを採用
        best = _pick_best(_pick_best(row_a, row_b), row_c)

        # --- スコアベースのラベル決定 ---
        # candidate には2種類ある:
        #   strict: パターン条件をすべて満たした候補
        #   score:  一部未達だが final_score と主要パターン適合度で昇格した候補
        candidate_source = "strict" if best["label"] == "candidate" else None
        final_score = scores["final_score"]
        score_pattern = _score_candidate_pattern(scores)
        if (
            final_score >= SCORE_CANDIDATE_MIN_FINAL
            and score_pattern
            and best["label"] != "candidate"
        ):
            best = pattern_rows[score_pattern]
            best["label"] = "candidate"
            candidate_source = "score"
        elif final_score >= SCORE_WATCH_MIN_FINAL and best["label"] == "skip":
            best["label"] = "watch"

        # 警戒モード時は新規買い候補を停止（分析ログとしては watch で保存）
        if market_mode in ("caution", "full_caution") and best["label"] == "candidate":
            best["label"] = "watch"
            candidate_source = None

        # スコアフィールドを行に追加
        best["trend_score"]      = scores["trend_score"]
        best["pattern_a_score"]  = scores["pattern_a_score"]
        best["pattern_b_score"]  = scores["pattern_b_score"]
        best["pattern_c_score"]  = scores["pattern_c_score"]
        best["liquidity_score"]  = scores["liquidity_score"]
        best["base_score"]       = scores["base_score"]
        best["market_multiplier"] = scores["market_multiplier"]
        best["final_score"]      = scores["final_score"]
        best["recommend_rank"]   = scores["recommend_rank"]
        best["recommend_reason"] = scores["recommend_reason"]
        best["market_mode"]      = market_mode
        best["_candidate_source"] = candidate_source

        all_rows.append(best)
        if best["label"] == "candidate":
            # 通知用テクニカルフィールドを tk から補完（entry_calculator 経由で notifier へ渡す）
            # daily_signals テーブルに存在しないため candidate のみに付与し、DB 保存後に追加する
            best["_notify_extras"] = {
                fld: tk.get(fld) for fld in ("ma5", "ma25", "ma75", "ma200", "high_52w")
            }
            # 攻めモード判定で参照する補助情報を保持
            best["_screened_extras"] = {
                "est_market_cap": tk.get("est_market_cap"),
                "return_5d": tk.get("return_5d"),
                "return_20d": tk.get("return_20d"),
                "vol_20d_avg": tk.get("vol_20d_avg"),
            }
            candidates.append(best)

    # 攻めモード時はハードフィルタを適用（除外銘柄は watch に降格）
    if is_offensive() and candidates:
        candidates = _apply_offensive_filters(candidates, target_str, all_rows)

    # final_score 降順でランク付け
    candidates.sort(key=lambda x: x.get("final_score") or 0, reverse=True)
    for rank, c in enumerate(candidates, start=1):
        c["rs_rank"] = rank

    # all_rows にも rs_rank を反映
    rank_map = {c["ticker"]: c["rs_rank"] for c in candidates}
    for row in all_rows:
        row["rs_rank"] = rank_map.get(row["ticker"])

    # DB 保存（500件ずつ）— _ プレフィックスは通知用の一時フィールド
    chunk = 500
    for i in range(0, len(all_rows), chunk):
        save_rows = [
            {k: v for k, v in r.items() if not k.startswith("_")}
            for r in all_rows[i:i + chunk]
        ]
        with get_connection() as conn:
            upsert(conn, "daily_signals", save_rows)

    # 通知用テクニカルフィールドを candidates に展開
    for c in candidates:
        extras = c.pop("_notify_extras", {})
        for fld, val in extras.items():
            if fld not in c or c[fld] is None:
                c[fld] = val
        # 攻めモード判定用の補助情報は破棄
        c.pop("_screened_extras", None)

    candidate_count = len(candidates)
    watch_count = sum(1 for r in all_rows if r["label"] == "watch")
    insufficient_str = f" / データ不足={data_insufficient_count} 件" if data_insufficient_count > 0 else ""
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
    return candidates[:_MAX_CANDIDATES]  # 最大件数に絞って返す


# ============================================================
# パターンA 評価
# ============================================================

def _eval_pattern_a(tk: dict, target_str: str, market: str) -> dict:
    """
    パターンA（モメンタムブレイクアウト）の全条件を評価する。
    全条件充足 → candidate、1〜2条件未充足 → watch、それ以外 → skip
    """
    close     = tk.get("close") or tk.get("adj_close")
    ma5       = tk.get("ma5")
    ma25      = tk.get("ma25")
    ma75      = tk.get("ma75")
    ma200     = tk.get("ma200")
    ma200_5d  = tk.get("ma200_5d_ago")
    vol       = tk.get("volume")
    vol_avg   = tk.get("vol_20d_avg")
    high_52w  = tk.get("high_52w")
    rsi       = tk.get("rsi14")

    # 各条件評価（None は「データ不足」として 0 扱い）
    # データ不足の条件を記録する
    missing = []
    if ma200 is None or ma200_5d is None:
        missing.append("MA200")
    if ma75 is None or ma25 is None:
        missing.append("MA整列")
    if high_52w is None:
        missing.append("52週高値")
    if vol is None or vol_avg is None:
        missing.append("出来高")
    if rsi is None:
        missing.append("RSI")
    if missing:
        logger.debug(f"{tk['ticker']}: データ不足のため判定保留 — {', '.join(missing)}")

    c1 = _flag(ma200 and ma200_5d and ma200 > ma200_5d)         # MA200上昇中
    c2 = _flag(ma75 and ma200 and ma25 and ma75 and
               ma75 >= ma200 and ma25 >= ma75 and
               (ma5 or 0) >= ma25)                              # MA整列
    c3 = _flag(close and high_52w and close >= high_52w * _PA_52W_PCT)  # 52週高値近傍
    c4 = _flag(vol and vol_avg and vol_avg > 0 and
               vol / vol_avg >= _PA_VOL_RATIO)                  # 出来高急増
    c5 = _flag(rsi and _PA_RSI_MIN <= rsi <= _PA_RSI_MAX)       # RSI適正
    c6 = _flag(close and ma25 and close >= ma25)                # MA25上方

    all_conds = [c1, c2, c3, c4, c5, c6]
    met = sum(all_conds)
    total = len(all_conds)

    if met == total:
        label = "candidate"
    elif met >= total - 2:
        label = "watch"
    else:
        label = "skip"

    vol_ratio = (vol / vol_avg) if (vol and vol_avg and vol_avg > 0) else None

    return {
        "ticker":  tk["ticker"],
        "date":    target_str,
        "market":  market,
        "pattern": "A",
        "label":   label,
        "cond_a_ma200_rising":  c1,
        "cond_a_ma_aligned":    c2,
        "cond_a_52w_high_near": c3,
        "cond_a_volume_surge":  c4,
        "cond_a_rsi_ok":        c5,
        "cond_a_above_ma25":    c6,
        "cond_b_ma200_rising":     None,
        "cond_b_ma_aligned":       None,
        "cond_b_pullback_touched": None,
        "cond_b_bounced":          None,
        "cond_b_ma25_rising":      None,
        "cond_b_rsi_ok":           None,
        "cond_c_weekly_uptrend":   None,
        "cond_c_at_support":       None,
        "cond_c_rsi_oversold":     None,
        "cond_c_sar_reversal":     None,
        "close":     close,
        "rsi14":     rsi,
        "atr14":     tk.get("atr14"),
        "vol_ratio": vol_ratio,
        "rs_rank":   None,
        "_conditions_met_count": met,
        "_conditions_total": total,
    }


# ============================================================
# パターンB 評価
# ============================================================

def _eval_pattern_b(tk: dict, target_str: str, market: str) -> dict:
    """
    パターンB（押し目買い）の全条件を評価する。
    押し目タッチの判定には price_history から過去5日分を参照する。
    """
    close     = tk.get("close") or tk.get("adj_close")
    ma25      = tk.get("ma25")
    ma75      = tk.get("ma75")
    ma200     = tk.get("ma200")
    ma200_5d  = tk.get("ma200_5d_ago")
    ma25_5d   = tk.get("ma25_5d_ago")
    vol       = tk.get("volume")
    vol_avg   = tk.get("vol_20d_avg")
    rsi       = tk.get("rsi14")

    # データ不足の条件を記録する
    missing = []
    if ma200 is None or ma200_5d is None:
        missing.append("MA200")
    if ma75 is None or ma25 is None:
        missing.append("MA整列")
    if ma25 is None or ma25_5d is None:
        missing.append("MA25推移")
    if rsi is None:
        missing.append("RSI")
    if missing:
        logger.debug(f"{tk['ticker']}: データ不足のため判定保留（B） — {', '.join(missing)}")

    c1 = _flag(ma200 and ma200_5d and ma200 > ma200_5d)         # MA200上昇中
    c2 = _flag(ma75 and ma200 and ma25 and
               ma75 >= ma200 and ma25 >= ma75)                  # MA整列
    c3, c4 = _check_pullback(tk["ticker"], target_str, ma25)    # 押し目タッチ・反発
    c5 = _flag(ma25 and ma25_5d and ma25 > ma25_5d)             # MA25上昇中
    c6 = _flag(rsi and _PB_RSI_MIN <= rsi <= _PB_RSI_MAX)       # RSI適正

    # 出来高はパターンBでは必須条件ではないが参考値として記録
    vol_ratio = (vol / vol_avg) if (vol and vol_avg and vol_avg > 0) else None

    all_conds = [c1, c2, c3, c4, c5, c6]
    met = sum(all_conds)
    total = len(all_conds)

    if met == total:
        label = "candidate"
    elif met >= total - 2:
        label = "watch"
    else:
        label = "skip"

    return {
        "ticker":  tk["ticker"],
        "date":    target_str,
        "market":  market,
        "pattern": "B",
        "label":   label,
        "cond_a_ma200_rising":  None,
        "cond_a_ma_aligned":    None,
        "cond_a_52w_high_near": None,
        "cond_a_volume_surge":  None,
        "cond_a_rsi_ok":        None,
        "cond_a_above_ma25":    None,
        "cond_b_ma200_rising":     c1,
        "cond_b_ma_aligned":       c2,
        "cond_b_pullback_touched": c3,
        "cond_b_bounced":          c4,
        "cond_b_ma25_rising":      c5,
        "cond_b_rsi_ok":           c6,
        "cond_c_weekly_uptrend":   None,
        "cond_c_at_support":       None,
        "cond_c_rsi_oversold":     None,
        "cond_c_sar_reversal":     None,
        "close":     close,
        "rsi14":     rsi,
        "atr14":     tk.get("atr14"),
        "vol_ratio": vol_ratio,
        "rs_rank":   None,
        "_conditions_met_count": met,
        "_conditions_total": total,
    }


def _check_pullback(
    ticker: str,
    target_str: str,
    ma25: Optional[float],
) -> tuple[int, int]:
    """
    直近 _PB_LOOKBACK 日間に MA25 へのタッチと反発があるか確認する。
    反発は直近 _PB_BOUNCE_DAYS 日間連続で終値が MA25 以上であることを要求する。
    返り値: (pullback_touched: int, bounced: int)
    """
    if not ma25:
        return 0, 0

    with get_connection() as conn:
        rows = fetch_all(
            conn,
            "SELECT date, low, adj_close FROM price_history "
            "WHERE ticker = ? AND date <= ? AND is_holiday = 0 "
            "ORDER BY date DESC LIMIT ?",
            (ticker, target_str, _PB_LOOKBACK + 1),
        )

    if len(rows) < 2:
        return 0, 0

    rows_asc = list(reversed(rows))  # 昇順に直す

    # 直近 _PB_LOOKBACK 日以内（今日を除く）に安値が MA25 × _PB_MA25_TOUCH 以下
    touched = False
    for r in rows_asc[:-1]:  # 最後（今日）を除く
        low = float(r["low"] or r["adj_close"] or 0)
        if low <= ma25 * _PB_MA25_TOUCH:
            touched = True
            break

    # 反発確認: 直近 _PB_BOUNCE_DAYS 日間連続で終値 >= MA25
    bounced = 0
    if touched:
        tail = rows_asc[-_PB_BOUNCE_DAYS:]  # 直近N日
        if len(tail) >= _PB_BOUNCE_DAYS and all(
            float(r["adj_close"] or 0) >= ma25 for r in tail
        ):
            bounced = 1
    return _flag(touched), bounced


# ============================================================
# パターンC 評価
# ============================================================

def _eval_pattern_c(tk: dict, target_str: str, market: str) -> dict:
    """
    パターンC（サポートバウンス）の全条件を評価する。

    条件:
    1. 週足MA25が上昇中（長期上昇トレンド確認）
    2. 終値がサポートレベル近傍（support_tolerance_pct 以内）
    3. RSI14が oversold 圏（≤ pattern_c_rsi_max）
    4. パラボリックSARが上昇方向に転換（sar_direction == 'up'）
    """
    close         = tk.get("close") or tk.get("adj_close")
    weekly_ma25   = tk.get("weekly_ma25")
    weekly_5w_ago = tk.get("weekly_ma25_5w_ago")
    support_level = tk.get("support_level")
    rsi           = tk.get("rsi14")
    sar_direction = tk.get("sar_direction")
    vol           = tk.get("volume")
    vol_avg       = tk.get("vol_20d_avg")

    # 各条件評価
    c1 = _flag(weekly_ma25 and weekly_5w_ago and weekly_ma25 > weekly_5w_ago)

    # サポート近傍: 終値が support_level の ±tolerance% 以内
    if close and support_level and support_level > 0:
        distance_pct = abs(close - support_level) / support_level * 100
        c2 = _flag(distance_pct <= _PC_SUPPORT_TOL)
    else:
        c2 = 0

    c3 = _flag(rsi is not None and rsi <= _PC_RSI_MAX)
    c4 = _flag(sar_direction == "up")

    vol_ratio = (vol / vol_avg) if (vol and vol_avg and vol_avg > 0) else None

    all_conds = [c1, c2, c3, c4]
    met = sum(all_conds)
    total = len(all_conds)

    if met == total:
        label = "candidate"
    elif met >= total - 1:
        label = "watch"
    else:
        label = "skip"

    return {
        "ticker":  tk["ticker"],
        "date":    target_str,
        "market":  market,
        "pattern": "C",
        "label":   label,
        "cond_a_ma200_rising":     None,
        "cond_a_ma_aligned":       None,
        "cond_a_52w_high_near":    None,
        "cond_a_volume_surge":     None,
        "cond_a_rsi_ok":           None,
        "cond_a_above_ma25":       None,
        "cond_b_ma200_rising":     None,
        "cond_b_ma_aligned":       None,
        "cond_b_pullback_touched": None,
        "cond_b_bounced":          None,
        "cond_b_ma25_rising":      None,
        "cond_b_rsi_ok":           None,
        "cond_c_weekly_uptrend":   c1,
        "cond_c_at_support":       c2,
        "cond_c_rsi_oversold":     c3,
        "cond_c_sar_reversal":     c4,
        "close":     close,
        "rsi14":     rsi,
        "atr14":     tk.get("atr14"),
        "vol_ratio": vol_ratio,
        "rs_rank":   None,
        "_conditions_met_count": met,
        "_conditions_total": total,
    }


# ============================================================
# 攻めモード用ハードフィルター
# ============================================================

_RANK_PRIORITY = {"S": 5, "A": 4, "B": 3, "C": 2, "Watch": 1}


def _apply_offensive_filters(
    candidates: list[dict],
    target_str: str,
    all_rows: list[dict],
) -> list[dict]:
    """
    攻めモード時の追加フィルタを適用する。
    除外した銘柄は all_rows 上のラベルを "watch" に降格する。
    """
    off_cfg = get_offensive_config()
    filters = off_cfg.get("filters", {})

    min_score = float(off_cfg.get("min_final_score", 80))
    min_rank = str(off_cfg.get("min_rank", "A")).upper()
    min_rank_pri = _RANK_PRIORITY.get(min_rank, 4)

    rsi_max = float(filters.get("rsi_max_hard", 80.0))
    block_upper = bool(filters.get("block_long_upper_shadow", True))
    upper_ratio_min = float(filters.get("upper_shadow_ratio_min", 0.6))
    block_red_surge = bool(filters.get("block_red_after_volume_surge", True))
    surge_pct = float(filters.get("block_recent_surge_pct", 30.0))
    surge_days = int(filters.get("block_recent_surge_days", 5))
    jp_max_mcap = float(filters.get("jp_max_market_cap_jpy", 500_000_000_000))
    us_max_mcap = float(filters.get("us_max_market_cap_usd", 30_000_000_000))

    tickers = [c["ticker"] for c in candidates]
    candle_map = _fetch_latest_candle(tickers, target_str) if tickers else {}

    kept: list[dict] = []
    rejected: list[tuple[str, str]] = []

    for c in candidates:
        sc = c.get("_screened_extras") or {}
        rsi = c.get("rsi14")
        score = c.get("final_score") or 0
        rank = c.get("recommend_rank") or "Watch"
        market = c.get("market")

        reason: str | None = None

        if score < min_score:
            reason = f"スコア{score} < {min_score}"
        elif _RANK_PRIORITY.get(rank, 0) < min_rank_pri:
            reason = f"ランク{rank} < {min_rank}"
        elif rsi is not None and rsi >= rsi_max:
            reason = f"RSI{rsi:.1f} >= {rsi_max}"

        # 直近5日急騰
        if reason is None and surge_pct > 0:
            ret_5d = sc.get("return_5d")
            if ret_5d is None:
                ret_5d = _calc_recent_return(c["ticker"], target_str, surge_days)
            if ret_5d is not None and ret_5d * 100.0 >= surge_pct:
                reason = f"直近{surge_days}日急騰{ret_5d * 100:.1f}% >= {surge_pct}%"

        # 時価総額上限
        if reason is None:
            mcap = sc.get("est_market_cap")
            if mcap is not None:
                if market == "JP" and mcap > jp_max_mcap:
                    reason = f"時価総額(JP) {mcap:,.0f} > {jp_max_mcap:,.0f}"
                elif market == "US" and mcap > us_max_mcap:
                    reason = f"時価総額(US) ${mcap:,.0f} > ${us_max_mcap:,.0f}"

        # 上ヒゲ / 出来高急増直後の陰線
        if reason is None and (block_upper or block_red_surge):
            candle = candle_map.get(c["ticker"])
            if candle:
                if block_upper and _is_long_upper_shadow(candle, upper_ratio_min):
                    reason = "長い上ヒゲ"
                elif block_red_surge and _is_red_after_volume_surge(candle, sc):
                    reason = "出来高急増直後の陰線"

        if reason is None:
            kept.append(c)
        else:
            rejected.append((c["ticker"], reason))

    if rejected:
        rejected_tickers = {t for t, _ in rejected}
        for row in all_rows:
            if row["ticker"] in rejected_tickers and row["label"] == "candidate":
                row["label"] = "watch"
                row["_candidate_source"] = None

        for t, r in rejected[:10]:
            logger.info(f"  攻めモードフィルター除外: {t} ({r})")
        if len(rejected) > 10:
            logger.info(f"  攻めモードフィルター除外: 他 {len(rejected) - 10} 件")

    logger.info(
        f"攻めモードフィルター適用: 候補 {len(candidates)} 件 → "
        f"{len(kept)} 件通過 / {len(rejected)} 件除外"
    )
    return kept


def _fetch_latest_candle(tickers: list[str], target_str: str) -> dict[str, dict]:
    """
    対象日の OHLCV を一括取得する。
    返り値: {ticker: {"open", "high", "low", "close", "volume"}}
    """
    if not tickers:
        return {}
    placeholders = ",".join(["?"] * len(tickers))
    sql = (
        f"SELECT ticker, open, high, low, adj_close AS close, volume "
        f"FROM price_history "
        f"WHERE ticker IN ({placeholders}) AND date = ? AND is_holiday = 0"
    )
    with get_connection() as conn:
        rows = fetch_all(conn, sql, tuple(tickers) + (target_str,))
    result: dict[str, dict] = {}
    for r in rows:
        result[r["ticker"]] = {
            "open": _safe_float(r["open"]),
            "high": _safe_float(r["high"]),
            "low": _safe_float(r["low"]),
            "close": _safe_float(r["close"]),
            "volume": _safe_float(r["volume"]),
        }
    return result


def _calc_recent_return(ticker: str, target_str: str, days: int) -> Optional[float]:
    """
    銘柄ごとに直近 days 営業日のリターンを取得（return_Nd が screened_tickers
    に無い場合のフォールバック）。
    """
    with get_connection() as conn:
        rows = fetch_all(
            conn,
            "SELECT adj_close FROM price_history "
            "WHERE ticker = ? AND date <= ? AND is_holiday = 0 "
            "ORDER BY date DESC LIMIT ?",
            (ticker, target_str, days + 1),
        )
    if len(rows) < 2:
        return None
    latest = _safe_float(rows[0]["adj_close"])
    base = _safe_float(rows[-1]["adj_close"])
    if not latest or not base or base <= 0:
        return None
    return (latest - base) / base


def _is_long_upper_shadow(candle: dict, ratio_min: float) -> bool:
    """
    長い上ヒゲ判定:
        (high - max(open, close)) / (high - low) >= ratio_min
    """
    high = candle.get("high")
    low = candle.get("low")
    open_ = candle.get("open")
    close = candle.get("close")
    if None in (high, low, open_, close):
        return False
    rng = high - low
    if rng <= 0:
        return False
    body_top = max(open_, close)
    upper_shadow = high - body_top
    return (upper_shadow / rng) >= ratio_min


def _is_red_after_volume_surge(candle: dict, sc: dict) -> bool:
    """
    出来高急増（20日平均比2倍以上）かつ陰線（close < open）を検出する。
    """
    open_ = candle.get("open")
    close = candle.get("close")
    volume = candle.get("volume")
    vol_avg = sc.get("vol_20d_avg")
    if None in (open_, close, volume, vol_avg):
        return False
    if vol_avg <= 0:
        return False
    return close < open_ and (volume / vol_avg) >= 2.0


def _safe_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ============================================================
# ユーティリティ
# ============================================================

def fetch_near_misses(
    target_date: date,
    market: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """
    通知対象にならなかった注目銘柄を取得する。

    以下の2種類を含む（final_score 降順）:
    1. candidate だが entry_calculator で除外された銘柄（損切り幅不足・資金不足等）
    2. watch ラベルの銘柄（スコア不足 or 警戒モード降格）
    """
    date_str = target_date.strftime("%Y-%m-%d")
    market_clause = " AND d.market = ?" if market else ""
    base_params: list = [date_str, market] if market else [date_str]

    sql = f"""
        SELECT d.*, u.name,
               CASE
                 WHEN d.label = 'candidate' THEN 'filtered'
                 WHEN d.market_mode IN ('caution', 'full_caution') AND d.final_score >= 50
                      THEN 'demoted'
                 ELSE 'score_short'
               END AS near_miss_type
        FROM daily_signals d
        LEFT JOIN universe_master u ON u.ticker = d.ticker
        WHERE d.date = ?{market_clause}
          AND (
            (d.label = 'candidate'
             AND d.ticker NOT IN (
               SELECT ticker FROM trade_signal_log WHERE signal_date = ?
             ))
            OR
            (d.label = 'watch' AND d.final_score >= 30)
          )
        ORDER BY d.final_score DESC
        LIMIT ?
    """
    params = base_params + [date_str, limit]
    with get_connection() as conn:
        rows = fetch_all(conn, sql, tuple(params))
    return [dict(r) for r in rows]


# 後方互換: 旧名でも呼び出し可能
fetch_watch_stocks = fetch_near_misses


def _flag(condition) -> int:
    """bool → 0 / 1 に変換する。None は 0 扱い。"""
    return 1 if condition else 0


def _score_candidate_pattern(scores: dict) -> Optional[str]:
    """スコア候補として採用できる最も強いパターン適合度を返す。"""
    fit = scores.get("pattern_fit")
    if isinstance(fit, dict):
        pattern_scores = (
            ("A", fit.get("A") or 0),
            ("B", fit.get("B") or 0),
            ("C", fit.get("C") or 0),
        )
    else:
        pattern_scores = (
            ("A", scores.get("pattern_a_score") or 0),
            ("B", scores.get("pattern_b_score") or 0),
            ("C", scores.get("pattern_c_score") or 0),
        )
    pattern, score = max(pattern_scores, key=lambda item: item[1])
    if score >= SCORE_CANDIDATE_MIN_PATTERN:
        return pattern
    return None


def _pick_best(row_a: dict, row_b: dict) -> dict:
    """
    2つのパターン評価結果のうち、より強いシグナルを持つ方を返す。
    candidate > watch > skip の優先順で選ぶ。
    同じ強度の場合は vol_ratio が高い方を優先する。
    3パターン以上の場合はチェーンして使用: _pick_best(_pick_best(a, b), c)
    """
    priority = {"candidate": 2, "watch": 1, "skip": 0}
    pa = priority[row_a["label"]]
    pb = priority[row_b["label"]]

    if pa > pb:
        return row_a
    if pb > pa:
        return row_b

    # 同順位 → vol_ratio が高い方
    va = row_a.get("vol_ratio") or 0
    vb = row_b.get("vol_ratio") or 0
    return row_a if va >= vb else row_b
