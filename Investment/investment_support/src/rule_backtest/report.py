"""バックテスト結果の集計・表示・ファイル出力。"""

from __future__ import annotations

import csv
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict
from datetime import date
from pathlib import Path

from .settings import BacktestSettings
from .simulator import SimulationResult

REASON_LABELS = {
    "stop_loss": "損切り",
    "tp_full": "利確（全量）",
    "tp_partial": "利確（半分）",
    "tp_extend_exit": "延長保有後の手仕舞い",
    "stale_exit": "長期停滞で手仕舞い",
    "price_jump": "価格の不連続で手仕舞い（分割の疑い）",
    "end_of_test": "期間終了時に保有中",
}

_COUNTER_LABELS = {
    "normal_days": "新規買いを判定した日（市場環境=通常）",
    "caution_days": "警戒で新規買い停止の日",
    "paused_days": "運用停止ルールで新規買い停止の日",
    "signals": "通知された候補",
    "skipped_entry_filter": "  └ 損切り幅フィルタで見送り",
    "skipped_held": "  └ 保有中のため見送り",
    "skipped_slots": "  └ 保有枠が満杯で見送り",
    "skipped_too_small": "  └ 資金不足（1単位も買えない）",
    "skipped_no_bar": "  └ 翌日に価格が無く見送り",
    "entries": "新規エントリー",
}

MARKET_LABELS = {"JP": "日本株", "US": "米国株"}
_INDEX_LABELS = {"N225": "日経平均", "SP500": "S&P500"}
_CURRENCY_UNITS = {"JPY": "円", "USD": "ドル"}


def summarize(result: SimulationResult, benchmarks: dict | None = None) -> dict:
    """
    日次評価額とトレード記録から成績指標を計算する。

    benchmarks: {市場: {"index", "return_pct", "yearly": {年: %}}}（指数の買い持ち比較用）
    金額は評価通貨（複数市場なら円）。
    """
    s = result.settings
    benchmarks = benchmarks or {}
    daily = result.daily
    initial = s.initial_capital
    final = daily[-1]["equity"] if daily else initial
    positions = aggregate_positions(result.trades, s.fx)
    total_return = (final / initial - 1) * 100 if initial > 0 else 0.0
    dd_pct, dd_date = max_drawdown(daily)
    bench = blended_benchmark_pct(s, benchmarks)

    return {
        "markets": list(s.markets),
        "currency": s.currency,
        "start": daily[0]["date"] if daily else s.start,
        "end": daily[-1]["date"] if daily else s.end,
        "trading_days": len(daily),
        "initial_capital": initial,
        "final_equity": final,
        "total_pnl": final - initial,
        "total_return_pct": total_return,
        "cagr_pct": cagr_pct(initial, final, daily),
        "max_drawdown_pct": dd_pct,
        "max_drawdown_date": dd_date,
        "benchmark_return_pct": bench,
        "excess_return_pct": total_return - bench if bench is not None else None,
        "benchmarks": benchmarks,
        **trade_stats(positions),
        "exit_reasons": dict(Counter(p["exit_reason"] for p in positions)),
        "partial_exits": sum(1 for t in result.trades if t["is_partial"]),
        "avg_positions_held": _mean([r["positions"] for r in daily]),
        "invested_days_pct": (
            sum(1 for r in daily if r["positions"] > 0) / len(daily) * 100 if daily else 0.0
        ),
        "by_strategy": group_stats(positions),
        "yearly": yearly_stats(initial, daily, positions, benchmarks),
        "monthly_returns": monthly_returns(initial, daily),
        "counters": {m: dict(c) for m, c in result.counters.items()},
        "first_pause": result.first_pause,
        "settings": asdict(s),
    }


def aggregate_positions(
    trades: list[dict],
    fx: Callable[[str], float] | None = None,
) -> list[dict]:
    """分割決済をまとめて、1エントリー = 1件の成績にする。pnl_base は評価通貨に換算した損益。"""
    grouped: dict[int, list[dict]] = {}
    for t in trades:
        grouped.setdefault(t["position_id"], []).append(t)
    positions = []
    for records in grouped.values():
        first, last = records[0], records[-1]
        market = first.get("market", "")
        cost = first["entry_price"] * first["initial_shares"]
        risk = (first["entry_price"] - first["stop_loss_price"]) * first["initial_shares"]
        pnl = sum(r["pnl_amount"] for r in records)
        positions.append({
            "position_id": first["position_id"],
            "market": market,
            "ticker": first["ticker"],
            "strategy": first["strategy"],
            "entry_date": first["entry_date"],
            "exit_date": last["exit_date"],
            "exit_reason": last["exit_reason"],
            "pnl_amount": pnl,
            "pnl_base": pnl * (fx(market) if fx else 1.0),
            "pnl_pct": pnl / cost * 100 if cost > 0 else 0.0,
            "r_multiple": pnl / risk if risk > 0 else None,
            "holding_days": last["holding_days"],
        })
    return positions


def trade_stats(positions: list[dict]) -> dict:
    """エントリー単位の勝率・期待値など。損益合計とプロフィットファクターは評価通貨。"""
    wins = [p for p in positions if p["pnl_amount"] > 0]
    losses = [p for p in positions if p["pnl_amount"] <= 0]
    gross_win = sum(p["pnl_base"] for p in wins)
    gross_loss = -sum(p["pnl_base"] for p in losses)
    return {
        "positions": len(positions),
        "win_rate_pct": len(wins) / len(positions) * 100 if positions else 0.0,
        "avg_win_pct": _mean([p["pnl_pct"] for p in wins]),
        "avg_loss_pct": _mean([p["pnl_pct"] for p in losses]),
        "expectancy_pct": _mean([p["pnl_pct"] for p in positions]),
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
        "avg_r_multiple": _mean([p["r_multiple"] for p in positions if p["r_multiple"] is not None]),
        "avg_holding_days": _mean([p["holding_days"] for p in positions]),
        "realized_pnl": sum(p["pnl_base"] for p in positions),
    }


def group_stats(positions: list[dict]) -> list[dict]:
    """市場 × 戦略ごとの成績。"""
    groups: dict[tuple[str, str], list[dict]] = {}
    for p in positions:
        groups.setdefault((p["market"], p["strategy"] or "-"), []).append(p)
    return [
        {"market": m, "strategy": st, **trade_stats(ps)}
        for (m, st), ps in sorted(groups.items())
    ]


def yearly_stats(
    initial: float,
    daily: list[dict],
    positions: list[dict],
    benchmarks: dict,
) -> list[dict]:
    """年ごとのリターン（前年末比。初年は初期資金比）・年内の最大ドローダウン・その年に買ったトレードの成績。"""
    by_year: dict[str, list[dict]] = {}
    for r in daily:
        by_year.setdefault(r["date"][:4], []).append(r)
    rows = []
    prev = initial
    for year, recs in by_year.items():
        end_equity = recs[-1]["equity"]
        dd, _ = max_drawdown([{"date": recs[0]["date"], "equity": prev}] + recs)
        stats = trade_stats([p for p in positions if p["entry_date"][:4] == year])
        rows.append({
            "year": year,
            "return_pct": (end_equity / prev - 1) * 100 if prev else 0.0,
            "max_drawdown_pct": dd,
            "entries": stats["positions"],
            "win_rate_pct": stats["win_rate_pct"],
            "expectancy_pct": stats["expectancy_pct"],
            "benchmarks": {m: b.get("yearly", {}).get(year) for m, b in benchmarks.items()},
        })
        prev = end_equity
    return rows


def blended_benchmark_pct(s: BacktestSettings, benchmarks: dict) -> float | None:
    """各市場の初期資金でその市場の指数を買い持ちした場合のリターン（評価通貨・固定レート）。"""
    if not benchmarks or s.initial_capital <= 0:
        return None
    total = 0.0
    for m in s.markets:
        ret = benchmarks.get(m, {}).get("return_pct")
        if ret is None:
            return None
        total += s.capital[m] * s.fx(m) * ret
    return total / s.initial_capital


def max_drawdown(daily: list[dict]) -> tuple[float, str | None]:
    """日次評価額の最大ドローダウン（%）と、その底の日付。"""
    peak = None
    worst = 0.0
    worst_date = None
    for r in daily:
        eq = r["equity"]
        peak = eq if peak is None or eq > peak else peak
        if peak > 0:
            dd = (peak - eq) / peak * 100
            if dd > worst:
                worst, worst_date = dd, r["date"]
    return worst, worst_date


def cagr_pct(initial: float, final: float, daily: list[dict]) -> float | None:
    if len(daily) < 2 or initial <= 0 or final <= 0:
        return None
    days = (date.fromisoformat(daily[-1]["date"]) - date.fromisoformat(daily[0]["date"])).days
    if days <= 0:
        return None
    return ((final / initial) ** (365.25 / days) - 1) * 100


def monthly_returns(initial: float, daily: list[dict]) -> list[dict]:
    """月末評価額の前月末比（初月は初期資金比）。"""
    month_end: dict[str, float] = {}
    for r in daily:
        month_end[r["date"][:7]] = r["equity"]
    out = []
    prev = initial
    for month, equity in month_end.items():
        out.append({"month": month, "equity": equity, "return_pct": (equity / prev - 1) * 100 if prev else 0.0})
        prev = equity
    return out


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


# ============================================================
# 表示
# ============================================================

def format_summary(summary: dict) -> str:
    unit = _CURRENCY_UNITS[summary["currency"]]
    st = summary["settings"]
    markets = summary["markets"]
    lines = [
        "=" * 72,
        f"  現行ルール バックテスト結果（{' + '.join(MARKET_LABELS[m] for m in markets)}）",
        "=" * 72,
        f"  期間            {summary['start']} ～ {summary['end']}（{summary['trading_days']} 日）",
        f"  初期資金        {summary['initial_capital']:,.0f} {unit}{_capital_breakdown(st, markets)}",
        f"  最終評価額      {summary['final_equity']:,.0f} {unit}"
        f"（損益 {summary['total_pnl']:+,.0f} {unit}）",
        f"  総リターン      {summary['total_return_pct']:+.2f}%",
        f"  年率換算        {_pct(summary['cagr_pct'])}",
        f"  最大ドローダウン {summary['max_drawdown_pct']:.2f}%（底: {summary['max_drawdown_date'] or '-'}）",
        f"  指数（買い持ち）{_pct(summary['benchmark_return_pct'])}{_benchmark_breakdown(summary)}"
        f"  → 差 {_pct(summary['excess_return_pct'])}",
        "",
        "  --- トレード成績（1エントリー単位、手数料・スリッページ込み） ---",
        f"  エントリー数    {summary['positions']}",
        f"  勝率            {summary['win_rate_pct']:.1f}%",
        f"  平均利益        {_pct(summary['avg_win_pct'])}   平均損失 {_pct(summary['avg_loss_pct'])}",
        f"  期待値          {_pct(summary['expectancy_pct'])}（1トレードあたり）",
        f"  平均 R          {_num(summary['avg_r_multiple'])}   プロフィットファクター {_num(summary['profit_factor'])}",
        f"  平均保有日数    {_num(summary['avg_holding_days'], 1)} 日",
        f"  平均保有銘柄数  {_num(summary['avg_positions_held'], 1)}"
        f"（保有ありの日 {summary['invested_days_pct']:.0f}%）",
    ]
    if summary["exit_reasons"]:
        lines += ["", "  --- 最終的な決済理由 ---"]
        for reason, count in sorted(summary["exit_reasons"].items(), key=lambda x: -x[1]):
            lines.append(f"  {REASON_LABELS.get(reason, reason):<28} {count}")
        lines.append(f"  （うち途中で半分利確したエントリーの分割決済 {summary['partial_exits']} 回）")

    lines += ["", "  --- 新規買いの判定内訳 ---"]
    for market, counters in summary["counters"].items():
        if len(markets) > 1:
            lines.append(f"  [{MARKET_LABELS[market]}]")
        for key, label in _COUNTER_LABELS.items():
            lines.append(f"  {label:<32} {counters.get(key, 0):,}")
    if summary["first_pause"]:
        fp = summary["first_pause"]
        reason = "月間損失" if fp["reason"] == "monthly_loss" else "連続損失"
        lines.append(f"  ※ 運用停止ルール（{reason}）が最初に発動: {fp['date']}（{MARKET_LABELS[fp['market']]}の判定時）")

    lines += [
        "",
        "  --- 条件 ---",
        f"  手数料 片道{st['commission_pct']}% / スリッページ 片道{st['slippage_pct']}% / "
        f"売買単位 {st['lot_size']}株",
        f"  保有上限 {st['max_holdings']}（全市場合計） / リスク {st['risk_pct'] * 100:g}% / "
        f"投入額の基準 {st['sizing']} / 損切り判定 {st['stop_mode']} / "
        f"運用停止ルール {'ON' if st['risk_control'] else 'OFF'}",
    ]
    if len(markets) > 1:
        lines.append(f"  為替 1ドル = {st['usdjpy']:g} 円（固定。評価額・月間損失の換算に使用）")
    lines.append("=" * 72)
    return "\n".join(lines)


def format_by_strategy(summary: dict) -> str:
    unit = _CURRENCY_UNITS[summary["currency"]]
    lines = ["  --- 市場・戦略別の成績 ---",
             f"  市場    戦略            件数   勝率    期待値     PF     損益合計（{unit}）"]
    for g in summary["by_strategy"]:
        lines.append(
            f"  {MARKET_LABELS.get(g['market'], g['market']):<6}  {_strategy_label(g['strategy']):<12}"
            f"  {g['positions']:>4}  {g['win_rate_pct']:5.1f}%  {_pct(g['expectancy_pct']):>8}"
            f"  {_num(g['profit_factor']):>5}  {g['realized_pnl']:>+12,.0f}"
        )
    return "\n".join(lines)


def format_yearly(summary: dict) -> str:
    bench_cols = [
        (m, _INDEX_LABELS.get(b["index"], b["index"])) for m, b in summary["benchmarks"].items()
    ]
    header = "  年      リターン   最大DD   エントリー   勝率    期待値"
    header += "".join(f"   {label}" for _, label in bench_cols)
    lines = ["  --- 年ごとの成績（指数は同じ期間の買い持ち） ---", header]
    for y in summary["yearly"]:
        row = (
            f"  {y['year']}  {y['return_pct']:+8.2f}%  {y['max_drawdown_pct']:6.2f}%"
            f"  {y['entries']:>8}  {y['win_rate_pct']:5.1f}%  {_pct(y['expectancy_pct']):>8}"
        )
        row += "".join(f"  {_pct(y['benchmarks'].get(m)):>8}" for m, _ in bench_cols)
        lines.append(row)
    return "\n".join(lines)


def format_monthly(summary: dict) -> str:
    lines = ["  月      評価額           月次リターン"]
    for m in summary["monthly_returns"]:
        lines.append(f"  {m['month']}  {m['equity']:>14,.0f}  {m['return_pct']:+7.2f}%")
    return "\n".join(lines)


def format_holdings_comparison(summaries: list[dict]) -> str:
    lines = [
        "  --- 保有上限の比較（他の条件は同じ） ---",
        "  保有上限  総リターン    年率    最大DD  エントリー   勝率    期待値     PF   枠不足で見送り",
    ]
    for s in summaries:
        skipped = sum(c.get("skipped_slots", 0) for c in s["counters"].values())
        lines.append(
            f"  {s['settings']['max_holdings']:>8}  {s['total_return_pct']:+9.2f}%  {_pct(s['cagr_pct']):>7}"
            f"  {s['max_drawdown_pct']:6.2f}%  {s['positions']:>9}  {s['win_rate_pct']:5.1f}%"
            f"  {_pct(s['expectancy_pct']):>8}  {_num(s['profit_factor']):>5}  {skipped:>12,}"
        )
    return "\n".join(lines)


def _capital_breakdown(st: dict, markets: list[str]) -> str:
    if len(markets) < 2:
        return ""
    parts = [
        f"{MARKET_LABELS[m]} {st['capital'][m]:,.0f} {'円' if m == 'JP' else 'ドル'}" for m in markets
    ]
    return f"（{' + '.join(parts)}）"


def _benchmark_breakdown(summary: dict) -> str:
    parts = [
        f"{_INDEX_LABELS.get(b['index'], b['index'])} {_pct(b['return_pct'])}"
        for b in summary["benchmarks"].values()
    ]
    if len(parts) < 2:
        return ""
    return f"（{' / '.join(parts)} を初期資金の比率で合成）"


def _strategy_label(name: str | None) -> str:
    if not name or name == "-":
        return "-"
    return "ブレイクアウト" if name.startswith("breakout") else "押し目スイング"


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v:+.2f}%"


def _num(v: float | None, digits: int = 2) -> str:
    return "-" if v is None else f"{v:.{digits}f}"


# ============================================================
# ファイル出力
# ============================================================

def write_outputs(result: SimulationResult, summary: dict, out_dir: Path) -> Path:
    """trades.csv（決済ごと）/ equity.csv（日次評価額）/ summary.json を書き出す。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / "trades.csv", result.trades)
    _write_csv(out_dir / "equity.csv", result.daily)
    with open(out_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return out_dir


def _write_csv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        if not rows:
            return
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
