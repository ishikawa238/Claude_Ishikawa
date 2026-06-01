"""
S&P500 ベンチマークとの資産推移比較

portfolio_snapshot（総資産）と index_snapshot（SP500）を正規化して比較する。
"""

from __future__ import annotations

from datetime import date, datetime

from src.config import CONFIG
from src.data_store import fetch_all, fetch_one, get_connection
from src.logger import get_logger

logger = get_logger(__name__)

_INDEX_CODE = "SP500"


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _monthly_deposit_jpy() -> float:
    goal = CONFIG.get("investment_goal", {})
    try:
        return float(goal.get("monthly_deposit_jpy", 10_000))
    except (TypeError, ValueError):
        return 10_000.0


def _deposit_adjustment_since(start: date, point: date) -> float:
    """開始日から point までに想定される累計入金額（毎月入金モデル）。"""
    if point <= start:
        return 0.0
    months = (point.year - start.year) * 12 + (point.month - start.month)
    return max(0, months) * _monthly_deposit_jpy()


def _fetch_daily_portfolio(conn, since: str) -> list[dict]:
    """日付ごとに最新のスナップショット（総資産・円換算）を返す。"""
    rows = fetch_all(
        conn,
        """
        SELECT snapshot_date, total_assets_combined_jpy
        FROM portfolio_snapshot
        WHERE snapshot_date >= ?
        ORDER BY snapshot_date ASC, created_at ASC
        """,
        (since,),
    )
    by_date: dict[str, float] = {}
    for row in rows:
        r = dict(row)
        d = str(r["snapshot_date"])[:10]
        val = r.get("total_assets_combined_jpy")
        if val is not None:
            by_date[d] = float(val)
    return [{"date": d, "assets_jpy": v} for d, v in sorted(by_date.items())]


def _sp500_close_on_or_before(conn, date_str: str) -> float | None:
    row = fetch_one(
        conn,
        """
        SELECT close FROM index_snapshot
        WHERE index_code = ? AND date <= ?
        ORDER BY date DESC LIMIT 1
        """,
        (_INDEX_CODE, date_str),
    )
    if not row:
        return None
    close = dict(row).get("close")
    return float(close) if close is not None else None


def _normalize_series(values: list[float]) -> list[float | None]:
    """先頭の有効値を 100 として正規化する。"""
    base = next((v for v in values if v is not None and v > 0), None)
    if base is None:
        return [None] * len(values)
    return [
        round(v / base * 100, 2) if v is not None and v > 0 else None
        for v in values
    ]


def build_benchmark_comparison(days: int = 365) -> dict:
    """
    ポートフォリオ vs S&P500 の比較データを構築する。

    返り値:
        dates, portfolio_index, portfolio_adjusted_index, sp500_index,
        summary (各種リターン・超過リターン), meta
    """
    days = max(30, min(int(days), 365 * 3))
    today = date.today()
    since = today.fromordinal(today.toordinal() - days).isoformat()

    with get_connection() as conn:
        portfolio_rows = _fetch_daily_portfolio(conn, since)

        if len(portfolio_rows) < 2:
            return {
                "dates": [],
                "portfolio_index": [],
                "portfolio_adjusted_index": [],
                "sp500_index": [],
                "summary": None,
                "meta": {
                    "days_requested": days,
                    "data_points": 0,
                    "message": "比較には portfolio_snapshot が2日分以上必要です（朝・夜バッチ実行後に記録されます）。",
                },
            }

        start_date = _parse_date(portfolio_rows[0]["date"])
        assets_raw = [r["assets_jpy"] for r in portfolio_rows]
        assets_adj = [
            r["assets_jpy"] - _deposit_adjustment_since(start_date, _parse_date(r["date"]))
            for r in portfolio_rows
        ]

        sp500_raw: list[float | None] = []
        for r in portfolio_rows:
            sp500_raw.append(_sp500_close_on_or_before(conn, r["date"]))

        dates = [r["date"] for r in portfolio_rows]
        portfolio_index = _normalize_series(assets_raw)
        portfolio_adjusted_index = _normalize_series(assets_adj)
        sp500_index = _normalize_series(sp500_raw)

    # サマリー（期間全体）
    first_assets = assets_raw[0]
    last_assets = assets_raw[-1]
    first_adj = assets_adj[0]
    last_adj = assets_adj[-1]
    sp_valid = [v for v in sp500_raw if v is not None and v > 0]
    first_sp = sp_valid[0] if sp_valid else None
    last_sp = sp_valid[-1] if sp_valid else None

    end_date = _parse_date(dates[-1])
    est_deposits = _deposit_adjustment_since(start_date, end_date)

    def _pct_change(start: float, end: float) -> float | None:
        if start <= 0:
            return None
        return round((end - start) / start * 100, 2)

    portfolio_return = _pct_change(first_assets, last_assets)
    portfolio_adj_return = _pct_change(first_adj, last_adj)
    sp500_return = _pct_change(first_sp, last_sp) if first_sp and last_sp else None

    excess_raw = None
    excess_adj = None
    if portfolio_return is not None and sp500_return is not None:
        excess_raw = round(portfolio_return - sp500_return, 2)
    if portfolio_adj_return is not None and sp500_return is not None:
        excess_adj = round(portfolio_adj_return - sp500_return, 2)

    summary = {
        "start_date": dates[0],
        "end_date": dates[-1],
        "portfolio_return_pct": portfolio_return,
        "portfolio_adjusted_return_pct": portfolio_adj_return,
        "sp500_return_pct": sp500_return,
        "excess_return_pct": excess_raw,
        "excess_adjusted_return_pct": excess_adj,
        "start_assets_jpy": round(first_assets),
        "end_assets_jpy": round(last_assets),
        "estimated_deposits_jpy": round(est_deposits),
        "monthly_deposit_jpy": round(_monthly_deposit_jpy()),
        "beating_sp500": (
            excess_adj > 0 if excess_adj is not None
            else (excess_raw > 0 if excess_raw is not None else None)
        ),
    }

    return {
        "dates": dates,
        "portfolio_index": portfolio_index,
        "portfolio_adjusted_index": portfolio_adjusted_index,
        "sp500_index": sp500_index,
        "summary": summary,
        "meta": {
            "days_requested": days,
            "data_points": len(dates),
            "index_base": 100,
            "message": None,
        },
    }
