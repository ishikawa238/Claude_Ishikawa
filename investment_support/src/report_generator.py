"""
月次レポート生成モジュール

役割:
- trade_result_log から当月の実績を集計する
- 勝率・平均損益・累積損益を算出する
- Slack Block Kit 形式のメッセージを生成する
"""

from datetime import date
from typing import Optional

from src.data_store import fetch_all, get_connection
from src.logger import get_logger
from src.notifier import _send

logger = get_logger(__name__)


# ============================================================
# 実績集計
# ============================================================

def aggregate_monthly(year: int, month: int) -> dict:
    """
    指定月の trade_result_log を集計する。

    返り値:
    {
        "year": 2025, "month": 1,
        "total_trades": 5,
        "win": 3, "loss": 2,
        "win_rate": 60.0,
        "avg_pnl_pct": 3.2,
        "total_pnl_amount": 15000.0,
        "best_trade":  {"ticker": "...", "pnl_pct": 12.5},
        "worst_trade": {"ticker": "...", "pnl_pct": -8.0},
        "trades": [...]
    }
    """
    month_start = f"{year:04d}-{month:02d}-01"
    # 月末は翌月1日未満で代用
    if month == 12:
        month_end = f"{year + 1:04d}-01-01"
    else:
        month_end = f"{year:04d}-{month + 1:02d}-01"

    with get_connection() as conn:
        trades = fetch_all(
            conn,
            "SELECT ticker, actual_exit_date, actual_exit_price, "
            "actual_entry_price, exit_reason, pnl_pct, pnl_amount, memo "
            "FROM trade_result_log "
            "WHERE actual_exit_date >= ? AND actual_exit_date < ? "
            "ORDER BY actual_exit_date",
            (month_start, month_end),
        )

    rows = [dict(r) for r in trades]
    total = len(rows)
    if total == 0:
        return {
            "year": year, "month": month,
            "total_trades": 0,
            "win": 0, "loss": 0,
            "win_rate": None,
            "avg_pnl_pct": None,
            "total_pnl_amount": None,
            "best_trade": None,
            "worst_trade": None,
            "trades": [],
        }

    pnl_pcts    = [r["pnl_pct"]    for r in rows if r["pnl_pct"]    is not None]
    pnl_amounts = [r["pnl_amount"] for r in rows if r["pnl_amount"] is not None]

    wins  = [p for p in pnl_pcts if p > 0]
    losses= [p for p in pnl_pcts if p <= 0]

    avg_pnl_pct     = round(sum(pnl_pcts) / len(pnl_pcts), 2) if pnl_pcts else None
    total_pnl_amount= round(sum(pnl_amounts), 2) if pnl_amounts else None
    win_rate        = round(len(wins) / len(pnl_pcts) * 100, 1) if pnl_pcts else None

    best_row  = max(rows, key=lambda r: r["pnl_pct"] or -999)
    worst_row = min(rows, key=lambda r: r["pnl_pct"] or 999)

    return {
        "year":             year,
        "month":            month,
        "total_trades":     total,
        "win":              len(wins),
        "loss":             len(losses),
        "win_rate":         win_rate,
        "avg_pnl_pct":      avg_pnl_pct,
        "total_pnl_amount": total_pnl_amount,
        "best_trade":       {"ticker": best_row["ticker"],  "pnl_pct": best_row["pnl_pct"]},
        "worst_trade":      {"ticker": worst_row["ticker"], "pnl_pct": worst_row["pnl_pct"]},
        "trades":           rows,
    }


# ============================================================
# Slack メッセージ生成・送信
# ============================================================

def send_monthly_report(year: int, month: int) -> None:
    """月次レポートを集計して Slack に送信する。"""
    stats = aggregate_monthly(year, month)
    blocks = _build_blocks(stats)

    report_date = date(year, month, 1)
    payload = {"text": f"{year}年{month}月 月次レポート", "blocks": blocks}
    result = _send(payload, report_date, "monthly")

    if result:
        logger.info(f"月次レポート送信完了: {year}年{month}月")
    else:
        logger.warning("月次レポートの Slack 送信に失敗しました。")


def _build_blocks(stats: dict) -> list:
    year  = stats["year"]
    month = stats["month"]

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📊 {year}年{month}月 月次レポート"}
        },
        {"type": "divider"},
    ]

    if stats["total_trades"] == 0:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "今月の決済済みトレードはありません。"}
        })
        return blocks

    # ── サマリー ──────────────────────────────────
    win_str  = f"{stats['win_rate']}%" if stats["win_rate"] is not None else "—"
    avg_str  = (f"{'+' if stats['avg_pnl_pct'] >= 0 else ''}{stats['avg_pnl_pct']}%"
                if stats["avg_pnl_pct"] is not None else "—")
    total_str= (f"{'+' if stats['total_pnl_amount'] >= 0 else ''}{stats['total_pnl_amount']:,.0f}"
                if stats["total_pnl_amount"] is not None else "—")

    blocks.append({
        "type": "section",
        "fields": [
            {"type": "mrkdwn", "text": f"*取引件数*\n{stats['total_trades']} 件 ({stats['win']}勝 {stats['loss']}敗)"},
            {"type": "mrkdwn", "text": f"*勝率*\n{win_str}"},
            {"type": "mrkdwn", "text": f"*平均損益率*\n{avg_str}"},
            {"type": "mrkdwn", "text": f"*合計損益*\n{total_str}"},
        ]
    })

    # ── ベスト・ワースト ───────────────────────────
    if stats["best_trade"]:
        best  = stats["best_trade"]
        worst = stats["worst_trade"]
        best_str  = f"{best['ticker']}  {'+' if best['pnl_pct'] >= 0 else ''}{best['pnl_pct']}%"  if best["pnl_pct"]  is not None else best["ticker"]
        worst_str = f"{worst['ticker']} {'+' if worst['pnl_pct'] >= 0 else ''}{worst['pnl_pct']}%" if worst["pnl_pct"] is not None else worst["ticker"]
        blocks.append({
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*最高トレード*\n{best_str}"},
                {"type": "mrkdwn", "text": f"*最低トレード*\n{worst_str}"},
            ]
        })

    # ── 個別明細 ──────────────────────────────────
    if stats["trades"]:
        lines = []
        for t in stats["trades"]:
            pct_str = (f"{'+' if t['pnl_pct'] >= 0 else ''}{t['pnl_pct']}%"
                       if t["pnl_pct"] is not None else "—")
            lines.append(f"• {t['actual_exit_date']}  {t['ticker']}  {pct_str}  ({t['exit_reason'] or '—'})")
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*決済明細*\n" + "\n".join(lines)}
        })

    return blocks
