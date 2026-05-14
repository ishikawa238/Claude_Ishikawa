"""
月次レポート生成モジュール

役割:
- trade_result_log から当月の実績を集計する
- 勝率・平均損益・累積損益を算出する
- パターン別期待値・警戒モード停止数・損切り実績・バッチ実行数を集計する
- Slack Block Kit 形式のメッセージを生成する
"""

from datetime import date
from typing import Optional

from src.config import CONFIG
from src.data_store import fetch_all, fetch_one, get_connection
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

def aggregate_system_stats(year: int, month: int) -> dict:
    """
    月次のシステム運用統計を集計する。

    返り値:
    {
        "caution_days":        警戒モードだった日数,
        "full_caution_days":   全面警戒だった日数,
        "normal_days":         通常モードだった日数,
        "signals_suppressed":  警戒モードで停止した候補数,
        "signals_total":       シグナル候補の合計数,
        "pattern_a_candidates": パターンA候補数,
        "pattern_b_candidates": パターンB候補数,
        "stop_loss_count":     損切り判定の回数,
        "batch_runs":          バッチ実行回数,
        "batch_failures":      バッチ失敗回数,
    }
    """
    month_start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        month_end = f"{year + 1:04d}-01-01"
    else:
        month_end = f"{year:04d}-{month + 1:02d}-01"

    with get_connection() as conn:
        # 市場環境モード別の日数
        env_rows = fetch_all(
            conn,
            "SELECT mode, COUNT(*) as cnt FROM market_environment "
            "WHERE date >= ? AND date < ? GROUP BY mode",
            (month_start, month_end),
        )
        env_map = {r["mode"]: r["cnt"] for r in env_rows}

        # 警戒モードで停止した候補数（watch に落とされた中で final_score >= 50 のもの）
        suppressed_row = fetch_one(
            conn,
            "SELECT COUNT(*) as cnt FROM daily_signals "
            "WHERE date >= ? AND date < ? "
            "  AND label = 'watch' AND market_mode IN ('caution', 'full_caution') "
            "  AND final_score >= 50",
            (month_start, month_end),
        )

        # シグナル候補の合計（trade_signal_log）
        signal_row = fetch_one(
            conn,
            "SELECT COUNT(*) as cnt FROM trade_signal_log "
            "WHERE signal_date >= ? AND signal_date < ?",
            (month_start, month_end),
        )

        # パターン別候補数
        pattern_rows = fetch_all(
            conn,
            "SELECT pattern, COUNT(*) as cnt FROM trade_signal_log "
            "WHERE signal_date >= ? AND signal_date < ? GROUP BY pattern",
            (month_start, month_end),
        )
        pattern_map = {r["pattern"]: r["cnt"] for r in pattern_rows}

        # 損切り判定回数（trade_result_log で exit_reason='stop_loss'）
        sl_row = fetch_one(
            conn,
            "SELECT COUNT(*) as cnt FROM trade_result_log "
            "WHERE actual_exit_date >= ? AND actual_exit_date < ? "
            "  AND exit_reason = 'stop_loss'",
            (month_start, month_end),
        )

        # バッチ実行数
        batch_rows = fetch_all(
            conn,
            "SELECT status, COUNT(*) as cnt FROM batch_run_log "
            "WHERE run_date >= ? AND run_date < ? GROUP BY status",
            (month_start, month_end),
        )
        batch_map = {r["status"]: r["cnt"] for r in batch_rows}

        # パターン別期待値 (trade_result_log)
        pattern_ev_rows = fetch_all(
            conn,
            "SELECT tsl.pattern, "
            "  COUNT(*) as cnt, "
            "  AVG(trl.pnl_pct) as avg_pnl, "
            "  SUM(CASE WHEN trl.pnl_pct > 0 THEN 1 ELSE 0 END) as wins, "
            "  AVG(CASE WHEN trl.pnl_pct > 0 THEN trl.pnl_pct END) as avg_win, "
            "  AVG(CASE WHEN trl.pnl_pct <= 0 THEN trl.pnl_pct END) as avg_loss "
            "FROM trade_result_log trl "
            "LEFT JOIN trade_signal_log tsl ON trl.signal_id = tsl.id "
            "WHERE trl.actual_exit_date >= ? AND trl.actual_exit_date < ? "
            "  AND trl.pnl_pct IS NOT NULL "
            "GROUP BY tsl.pattern",
            (month_start, month_end),
        )
        pattern_ev = {}
        for r in pattern_ev_rows:
            pat = r["pattern"] or "manual"
            cnt = r["cnt"]
            wr = r["wins"] / cnt if cnt > 0 else 0
            avg_win = r["avg_win"] or 0
            avg_loss = r["avg_loss"] or 0
            ev = wr * avg_win + (1 - wr) * avg_loss
            pattern_ev[pat] = {
                "count": cnt,
                "win_rate": round(wr * 100, 1),
                "ev": round(ev, 2),
            }

        # 損切り遵守による損失削減額
        sl_savings_row = fetch_one(
            conn,
            "SELECT SUM(ABS(trl.pnl_amount)) as total_loss "
            "FROM trade_result_log trl "
            "WHERE trl.actual_exit_date >= ? AND trl.actual_exit_date < ? "
            "  AND trl.exit_reason = 'stop_loss' AND trl.pnl_amount < 0",
            (month_start, month_end),
        )
        sl_total_loss = sl_savings_row["total_loss"] if sl_savings_row and sl_savings_row["total_loss"] else 0

    # コスト比率（月額コスト ÷ 総資金）
    capital_jpy = CONFIG.get("capital", {}).get("total_capital_jpy", 0)
    capital_usd = CONFIG.get("capital", {}).get("total_capital_usd", 0)
    usdjpy = CONFIG.get("capital", {}).get("usdjpy_fallback", 150.0)
    total_capital = capital_jpy + capital_usd * usdjpy
    monthly_cost = 19.99 * usdjpy  # EODHD $19.99/月
    cost_ratio = round(monthly_cost / total_capital * 100, 2) if total_capital > 0 else None

    return {
        "caution_days":        env_map.get("caution", 0),
        "full_caution_days":   env_map.get("full_caution", 0),
        "normal_days":         env_map.get("normal", 0),
        "signals_suppressed":  suppressed_row["cnt"] if suppressed_row else 0,
        "signals_total":       signal_row["cnt"] if signal_row else 0,
        "pattern_a_candidates": pattern_map.get("A", 0),
        "pattern_b_candidates": pattern_map.get("B", 0),
        "stop_loss_count":     sl_row["cnt"] if sl_row else 0,
        "batch_runs":          sum(batch_map.values()) if batch_map else 0,
        "batch_failures":      batch_map.get("failed", 0) + batch_map.get("partial_failure", 0),
        "pattern_ev":          pattern_ev,
        "sl_total_loss":       round(sl_total_loss, 0),
        "cost_ratio":          cost_ratio,
        "monthly_cost":        round(monthly_cost, 0),
    }


def send_monthly_report(year: int, month: int) -> None:
    """月次レポートを集計して Slack に送信する。"""
    stats = aggregate_monthly(year, month)
    sys_stats = aggregate_system_stats(year, month)
    blocks = _build_blocks(stats, sys_stats)

    report_date = date(year, month, 1)
    payload = {"text": f"{year}年{month}月 月次レポート", "blocks": blocks}
    result = _send(payload, report_date, "monthly")

    if result:
        logger.info(f"月次レポート送信完了: {year}年{month}月")
    else:
        logger.warning("月次レポートの Slack 送信に失敗しました。")


def _build_blocks(stats: dict, sys_stats: dict | None = None) -> list:
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
    else:
        _append_trade_summary(blocks, stats)

    # ── システム運用統計 ──────────────────────────
    if sys_stats:
        _append_system_stats(blocks, sys_stats)

    return blocks


def _append_trade_summary(blocks: list, stats: dict) -> None:
    """取引実績サマリーをブロックに追加する。"""
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


def _append_system_stats(blocks: list, ss: dict) -> None:
    """システム運用統計をブロックに追加する。"""
    blocks.append({"type": "divider"})
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": "🔧 システム運用統計"}
    })

    # 市場環境
    total_days = ss["normal_days"] + ss["caution_days"] + ss["full_caution_days"]
    env_text = (
        f"*市場環境*\n"
        f"通常: {ss['normal_days']}日 / 警戒: {ss['caution_days']}日 / "
        f"全面警戒: {ss['full_caution_days']}日"
    )
    if total_days > 0:
        caution_ratio = (ss["caution_days"] + ss["full_caution_days"]) / total_days * 100
        env_text += f"\n警戒比率: {caution_ratio:.0f}%"

    # シグナル統計
    signal_text = (
        f"*シグナル統計*\n"
        f"通知済み候補: {ss['signals_total']}件 "
        f"(A: {ss['pattern_a_candidates']} / B: {ss['pattern_b_candidates']})\n"
        f"警戒モードで停止: {ss['signals_suppressed']}件"
    )

    blocks.append({
        "type": "section",
        "fields": [
            {"type": "mrkdwn", "text": env_text},
            {"type": "mrkdwn", "text": signal_text},
        ]
    })

    # パターン別期待値
    pattern_ev = ss.get("pattern_ev", {})
    if pattern_ev:
        ev_lines = ["*パターン別期待値*"]
        for pat in sorted(pattern_ev.keys()):
            info = pattern_ev[pat]
            pat_label = {"A": "A (ブレイクアウト)", "B": "B (押し目買い)"}.get(pat, pat)
            ev_sign = "+" if info["ev"] >= 0 else ""
            ev_lines.append(
                f"  {pat_label}: {info['count']}件 "
                f"勝率{info['win_rate']}% "
                f"EV {ev_sign}{info['ev']}%"
            )
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(ev_lines)},
        })

    # 損切り・コスト
    sl_text = f"*損切り実行*\n{ss['stop_loss_count']}件"
    if ss.get("sl_total_loss", 0) > 0:
        sl_text += f"\n損切りによる確定損失: {ss['sl_total_loss']:,.0f}円"
        sl_text += "\n（SLなしなら更に拡大した可能性あり）"

    cost_text = f"*バッチ実行*\n{ss['batch_runs']}回 (失敗: {ss['batch_failures']}回)"
    if ss.get("cost_ratio") is not None:
        cost_text += (
            f"\n*月額コスト*\n"
            f"約{ss['monthly_cost']:,.0f}円 "
            f"(資金比 {ss['cost_ratio']}%)"
        )

    blocks.append({
        "type": "section",
        "fields": [
            {"type": "mrkdwn", "text": sl_text},
            {"type": "mrkdwn", "text": cost_text},
        ]
    })

    # 判断ガイド
    guide_lines = []
    if ss["signals_total"] == 0 and ss["normal_days"] > 5:
        guide_lines.append("通常モードでも候補なし → パラメータ緩和を検討")
    if ss["signals_suppressed"] > 10:
        guide_lines.append(f"警戒モードで{ss['signals_suppressed']}件停止 → 市場回復後の再開判断に注意")
    if ss["batch_failures"] > 0:
        guide_lines.append(f"バッチ失敗{ss['batch_failures']}回 → ログを確認してください")
    if ss.get("cost_ratio") is not None and ss["cost_ratio"] > 5.0:
        guide_lines.append(f"月額コスト比率 {ss['cost_ratio']}% → 資金規模に対してコストが高め。増資または縮小を検討")

    if guide_lines:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*💡 改善のヒント*\n" + "\n".join(f"• {g}" for g in guide_lines),
            }
        })
