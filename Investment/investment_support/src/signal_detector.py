"""
売買シグナル検出モジュール

パターンA: モメンタムブレイクアウト
パターンB: 押し目買い

各条件の充足状況をフラグで記録し、daily_signals テーブルに保存する。
市場環境モードにかかわらずシグナル検出を実施する。
market_mode は entry_calculator を通じて trade_signal_log に記録される。
"""

from datetime import date
from typing import Optional

from src.config import CONFIG
from src.data_store import fetch_all, get_connection, upsert
from src.logger import get_logger
from src.scoring import compute_score, fetch_recent_lows

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

        # どちらかが candidate なら採用
        best = _pick_best(row_a, row_b)

        # --- スコアベースのラベル決定 ---
        # final_score >= 50 → candidate に昇格（既存 AND 条件を緩和）
        final_score = scores["final_score"]
        if final_score >= 50 and best["label"] != "candidate":
            best["label"] = "candidate"
        elif final_score >= 30 and best["label"] == "skip":
            best["label"] = "watch"

        # 警戒モード時は新規買い候補を停止（分析ログとしては watch で保存）
        if market_mode in ("caution", "full_caution") and best["label"] == "candidate":
            best["label"] = "watch"

        # スコアフィールドを行に追加
        best["trend_score"]      = scores["trend_score"]
        best["pattern_a_score"]  = scores["pattern_a_score"]
        best["pattern_b_score"]  = scores["pattern_b_score"]
        best["liquidity_score"]  = scores["liquidity_score"]
        best["base_score"]       = scores["base_score"]
        best["market_multiplier"] = scores["market_multiplier"]
        best["final_score"]      = scores["final_score"]
        best["recommend_rank"]   = scores["recommend_rank"]
        best["recommend_reason"] = scores["recommend_reason"]
        best["market_mode"]      = market_mode

        all_rows.append(best)
        if best["label"] == "candidate":
            # 通知用テクニカルフィールドを tk から補完（entry_calculator 経由で notifier へ渡す）
            # daily_signals テーブルに存在しないため candidate のみに付与し、DB 保存後に追加する
            best["_notify_extras"] = {
                fld: tk.get(fld) for fld in ("ma5", "ma25", "ma75", "ma200", "high_52w")
            }
            candidates.append(best)

    # final_score 降順でランク付け
    candidates.sort(key=lambda x: x.get("final_score") or 0, reverse=True)
    for rank, c in enumerate(candidates, start=1):
        c["rs_rank"] = rank

    # all_rows にも rs_rank を反映
    rank_map = {c["ticker"]: c["rs_rank"] for c in candidates}
    for row in all_rows:
        row["rs_rank"] = rank_map.get(row["ticker"])

    # DB 保存（500件ずつ）— _notify_extras は daily_signals に無いため除外
    chunk = 500
    for i in range(0, len(all_rows), chunk):
        save_rows = [{k: v for k, v in r.items() if k != "_notify_extras"} for r in all_rows[i:i + chunk]]
        with get_connection() as conn:
            upsert(conn, "daily_signals", save_rows)

    # 通知用テクニカルフィールドを candidates に展開
    for c in candidates:
        extras = c.pop("_notify_extras", {})
        for fld, val in extras.items():
            if fld not in c or c[fld] is None:
                c[fld] = val

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
        "close":     close,
        "rsi14":     rsi,
        "atr14":     tk.get("atr14"),
        "vol_ratio": vol_ratio,
        "rs_rank":   None,
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
        "close":     close,
        "rsi14":     rsi,
        "atr14":     tk.get("atr14"),
        "vol_ratio": vol_ratio,
        "rs_rank":   None,
    }


def _check_pullback(
    ticker: str,
    target_str: str,
    ma25: Optional[float],
) -> tuple[int, int]:
    """
    直近 _PB_LOOKBACK 日間に MA25 へのタッチと反発があるか確認する。
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
    today_close = float(rows_asc[-1]["adj_close"] or 0)

    # 直近 _PB_LOOKBACK 日以内（今日を除く）に安値が MA25 × _PB_MA25_TOUCH 以下
    touched = False
    for r in rows_asc[:-1]:  # 最後（今日）を除く
        low = float(r["low"] or r["adj_close"] or 0)
        if low <= ma25 * _PB_MA25_TOUCH:
            touched = True
            break

    bounced = _flag(today_close >= ma25) if touched else 0
    return _flag(touched), bounced


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
        return fetch_all(conn, sql, tuple(params))


# 後方互換: 旧名でも呼び出し可能
fetch_watch_stocks = fetch_near_misses


def _flag(condition) -> int:
    """bool → 0 / 1 に変換する。None は 0 扱い。"""
    return 1 if condition else 0


def _pick_best(row_a: dict, row_b: dict) -> dict:
    """
    パターンA・B のうち、より強いシグナルを持つ方を返す。
    candidate > watch > skip の優先順で選ぶ。
    同じ強度の場合は vol_ratio が高い方を優先する。
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
