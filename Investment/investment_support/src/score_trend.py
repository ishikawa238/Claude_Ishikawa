"""
銘柄スコア（final_score）の短期トレンド判定

daily_signals の履歴から、直近スコアが上昇・下降・横ばいかを判定する。
"""

from __future__ import annotations

from datetime import date, timedelta
from html import escape

from src.data_store import fetch_all

# この点数差以上で上昇/下降とみなす（未満は横ばい）
SCORE_TREND_THRESHOLD = 2.0
# 比較対象とする過去のスナップショット数（営業日ベース・最大）
DEFAULT_LOOKBACK_SNAPSHOTS = 5


def compute_score_trend(scores_ascending: list[float]) -> dict:
    """
    古い順の final_score リストからトレンドを算出する。

    返り値:
        direction: up | down | flat | unknown
        delta: 最新 − 比較基準（点）
        compare_days: 比較に使った営業日スナップショット間隔
        latest, previous: 比較に使った値
    """
    if len(scores_ascending) < 2:
        return {
            "direction": "unknown",
            "delta": None,
            "compare_days": 0,
            "latest": scores_ascending[-1] if scores_ascending else None,
            "previous": None,
        }

    latest = float(scores_ascending[-1])
    back = min(DEFAULT_LOOKBACK_SNAPSHOTS, len(scores_ascending) - 1)
    previous = float(scores_ascending[-1 - back])
    compare_days = back
    delta = round(latest - previous, 1)

    if delta >= SCORE_TREND_THRESHOLD:
        direction = "up"
    elif delta <= -SCORE_TREND_THRESHOLD:
        direction = "down"
    else:
        direction = "flat"

    return {
        "direction": direction,
        "delta": delta,
        "compare_days": compare_days,
        "latest": latest,
        "previous": previous,
    }


def fetch_score_history(
    conn,
    ticker: str,
    *,
    limit: int = 14,
) -> list[dict]:
    """銘柄の日次スコア履歴（古い順）。"""
    rows = fetch_all(
        conn,
        """
        SELECT date, final_score
        FROM daily_signals
        WHERE ticker = ? AND final_score IS NOT NULL
        ORDER BY date DESC
        LIMIT ?
        """,
        (ticker, limit),
    )
    out = [
        {"date": str(dict(r)["date"])[:10], "final_score": float(dict(r)["final_score"])}
        for r in reversed(rows)
    ]
    return out


def fetch_score_trends_for_tickers(
    conn,
    tickers: list[str],
    *,
    history_limit: int = 14,
) -> dict[str, dict]:
    """複数ティッカーのスコアトレンドを一括取得。"""
    tickers = [t.strip() for t in tickers if t and str(t).strip()]
    if not tickers:
        return {}

    placeholders = ",".join("?" * len(tickers))
    since = (date.today() - timedelta(days=120)).isoformat()
    rows = fetch_all(
        conn,
        f"""
        SELECT ticker, date, final_score
        FROM daily_signals
        WHERE ticker IN ({placeholders})
          AND final_score IS NOT NULL
          AND date >= ?
        ORDER BY ticker, date ASC
        """,
        (*tickers, since),
    )

    by_ticker: dict[str, list[float]] = {t: [] for t in tickers}
    for row in rows:
        r = dict(row)
        t = r["ticker"]
        if t in by_ticker:
            by_ticker[t].append(float(r["final_score"]))

    result: dict[str, dict] = {}
    for t, scores in by_ticker.items():
        if len(scores) > history_limit:
            scores = scores[-history_limit:]
        result[t] = compute_score_trend(scores)
    return result


def score_trend_label(direction: str) -> str:
    return {
        "up": "上昇",
        "down": "下降",
        "flat": "横ばい",
        "unknown": "—",
    }.get(direction, "—")


def format_score_trend_badge(trend: dict | None) -> str:
    """スコア横に付けるトレンドバッジ HTML。"""
    if not trend or trend.get("direction") == "unknown":
        return ""

    direction = trend["direction"]
    delta = trend.get("delta")
    days = trend.get("compare_days") or 0
    css = f"score-trend score-trend-{direction}"

    if direction == "up":
        arrow = "↑"
    elif direction == "down":
        arrow = "↓"
    else:
        arrow = "→"

    if delta is not None:
        sign = "+" if delta > 0 else ""
        delta_txt = f"{sign}{delta:.0f}"
    else:
        delta_txt = ""

    tip = score_trend_label(direction)
    if days > 0 and delta is not None:
        tip += f"（{days}営業日前比 {delta_txt}点）"

    body = f"{arrow}{delta_txt}" if delta_txt else arrow
    return (
        f'<span class="{css}" title="{escape(tip)}">{escape(body)}</span>'
    )


def format_score_cell_html(
    score: float | None,
    trend: dict | None,
    *,
    score_str: str | None = None,
) -> str:
    """スコア数値 + トレンドバッジ。"""
    display = score_str if score_str is not None else (
        f"{score:.0f}" if score is not None else "—"
    )
    badge = format_score_trend_badge(trend)
    if not badge:
        return escape(display)
    return f'<span class="score-cell">{escape(display)}{badge}</span>'
