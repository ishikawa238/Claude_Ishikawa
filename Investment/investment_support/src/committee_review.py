"""
投資委員会 判定の事後検証モジュール

committee_verdict_log の各判定について、判定後 N 営業日の値動きを price_history から集計する。
比較対象（ベースライン）は同期間の全エントリーセット（trade_signal_log）。

評価方法:
- 基準価格: 判定日以前の直近終値（朝バッチの米国株は前営業日終値、夜バッチの日本株は当日終値）
- N 日リターン: 基準日から N 営業日後の終値 / 基準価格 - 1
- 損切り/利確の先着: 買い候補のみ。最長ホライズン内で安値が損切り、高値が利確に先に届いたか
  （同日に両方届いた場合は保守的に損切りとみなす）
- 方向の正否: BUY / HOLD は上昇（HOLD は 0 以上）、WAIT / SKIP / REDUCE / SELL は下落（BUY 見送りは 0 以下）で正解
- 株式分割の疑い: 前日比 0.4 倍未満 / 2.5 倍超の日を含む期間は評価から除外（price_history は分割未調整）
"""

from collections import defaultdict
from typing import Optional

from src.data_store import fetch_all, fetch_one, get_connection
from src.logger import get_logger

logger = get_logger(__name__)

DEFAULT_HORIZONS = (5, 20)
_BULLISH = {"BUY", "HOLD"}
_SPLIT_RATIO_MIN = 0.4
_SPLIT_RATIO_MAX = 2.5


def evaluate_verdicts(since: Optional[str] = None, horizons: tuple[int, ...] = DEFAULT_HORIZONS) -> list[dict]:
    """委員会判定ごとの事後成績を返す。"""
    sql = (
        "SELECT verdict_date, batch_type, ticker, market, subject_type, verdict, confidence "
        "FROM committee_verdict_log WHERE status IN ('full', 'partial') AND verdict IS NOT NULL"
    )
    params: tuple = ()
    if since:
        sql += " AND verdict_date >= ?"
        params = (since,)
    sql += " ORDER BY verdict_date, id"

    with get_connection() as conn:
        rows = [dict(r) for r in fetch_all(conn, sql, params)]
        results = []
        for r in rows:
            verdict = str(r["verdict"]).upper()
            levels = _entry_levels(conn, r["ticker"], r["verdict_date"]) if r["subject_type"] == "buy_candidate" else None
            ev = _evaluate(conn, r["ticker"], r["verdict_date"], horizons, levels)
            ev.update({
                "date": r["verdict_date"],
                "ticker": r["ticker"],
                "market": r["market"],
                "subject_type": r["subject_type"],
                "verdict": verdict,
                "confidence": r["confidence"],
            })
            for h in horizons:
                ret = ev.get(f"ret_{h}")
                ev[f"correct_{h}"] = None if ret is None else _is_correct(verdict, r["subject_type"], ret)
            results.append(ev)
    return results


def evaluate_baseline(since: Optional[str] = None, horizons: tuple[int, ...] = DEFAULT_HORIZONS) -> list[dict]:
    """全エントリーセット（委員会を通さない場合の成績）を返す。"""
    sql = "SELECT signal_date, ticker, market, stop_loss_price, target_price FROM trade_signal_log"
    params: tuple = ()
    if since:
        sql += " WHERE signal_date >= ?"
        params = (since,)
    with get_connection() as conn:
        rows = [dict(r) for r in fetch_all(conn, sql, params)]
        results = []
        for r in rows:
            levels = (r["stop_loss_price"], r["target_price"])
            ev = _evaluate(conn, r["ticker"], r["signal_date"], horizons, levels)
            ev.update({"date": r["signal_date"], "ticker": r["ticker"], "market": r["market"]})
            results.append(ev)
    return results


def summarize(evals: list[dict], horizons: tuple[int, ...], key=None) -> dict:
    """グループごとに件数・平均リターン・勝率・正解率・損切り/利確の先着を集計する。"""
    groups: dict = defaultdict(list)
    for ev in evals:
        groups[key(ev) if key else "ALL"].append(ev)

    summary = {}
    for name, items in groups.items():
        s = {"count": len(items)}
        for h in horizons:
            rets = [e[f"ret_{h}"] for e in items if e.get(f"ret_{h}") is not None]
            corrects = [e[f"correct_{h}"] for e in items if e.get(f"correct_{h}") is not None]
            s[f"n_{h}"] = len(rets)
            s[f"avg_{h}"] = round(sum(rets) / len(rets) * 100, 2) if rets else None
            s[f"win_{h}"] = round(sum(1 for x in rets if x > 0) / len(rets) * 100, 1) if rets else None
            s[f"acc_{h}"] = round(sum(corrects) / len(corrects) * 100, 1) if corrects else None
        outcomes = [e.get("outcome") for e in items]
        s["target"] = outcomes.count("target")
        s["stop"] = outcomes.count("stop")
        s["split_suspect"] = sum(1 for e in items if e.get("split_suspect"))
        summary[name] = s
    return summary


def format_report(verdicts: list[dict], baseline: list[dict], horizons: tuple[int, ...]) -> str:
    """CLI 表示用のレポート文字列を返す。"""
    lines = ["=" * 72, "  投資委員会 判定の事後検証", "=" * 72]
    if not verdicts:
        lines.append("  評価対象の判定がありません（committee_verdict_log が空）")
    by_verdict = summarize(verdicts, horizons, key=lambda e: f"{e['subject_type']}:{e['verdict']}")
    lines.append(_format_table("判定別", by_verdict, horizons))
    lines.append(_format_table("ベースライン（全エントリーセット）", summarize(baseline, horizons), horizons))
    lines.append("  avg=平均リターン% / win=上昇した割合% / acc=判定の方向が当たった割合% / 未経過の判定は n に含まない")
    lines.append("=" * 72)
    return "\n".join(lines)


# ============================================================
# ヘルパー
# ============================================================

def _evaluate(conn, ticker: str, date_str: str, horizons: tuple[int, ...], levels: Optional[tuple]) -> dict:
    base = fetch_one(
        conn,
        "SELECT date, close FROM price_history "
        "WHERE ticker = ? AND date <= ? AND is_holiday = 0 AND close IS NOT NULL "
        "ORDER BY date DESC LIMIT 1",
        (ticker, date_str),
    )
    result: dict = {f"ret_{h}": None for h in horizons}
    result["outcome"] = None
    if not base or not base["close"]:
        return result

    base_close = float(base["close"])
    forward = [dict(r) for r in fetch_all(
        conn,
        "SELECT date, high, low, close FROM price_history "
        "WHERE ticker = ? AND date > ? AND is_holiday = 0 AND close IS NOT NULL "
        "ORDER BY date ASC LIMIT ?",
        (ticker, base["date"], max(horizons)),
    )]
    if _has_price_gap([base_close] + [float(d["close"]) for d in forward]):
        result["split_suspect"] = True
        return result
    for h in horizons:
        if len(forward) >= h:
            result[f"ret_{h}"] = float(forward[h - 1]["close"]) / base_close - 1

    if levels and levels[0] and levels[1]:
        result["outcome"] = _first_touch(forward, float(levels[0]), float(levels[1]))
    return result


def _has_price_gap(closes: list[float]) -> bool:
    """price_history は分割未調整のため、前日比が極端な日を含む期間は評価しない。"""
    for prev, cur in zip(closes, closes[1:]):
        if prev > 0 and not (_SPLIT_RATIO_MIN <= cur / prev <= _SPLIT_RATIO_MAX):
            return True
    return False


def _first_touch(forward: list[dict], stop: float, target: float) -> Optional[str]:
    for day in forward:
        low = day.get("low") if day.get("low") is not None else day["close"]
        high = day.get("high") if day.get("high") is not None else day["close"]
        if float(low) <= stop:
            return "stop"
        if float(high) >= target:
            return "target"
    return None


def _entry_levels(conn, ticker: str, date_str: str) -> Optional[tuple]:
    row = fetch_one(
        conn,
        "SELECT stop_loss_price, target_price FROM trade_signal_log "
        "WHERE ticker = ? AND signal_date <= ? ORDER BY signal_date DESC, id DESC LIMIT 1",
        (ticker, date_str),
    )
    return (row["stop_loss_price"], row["target_price"]) if row else None


def _is_correct(verdict: str, subject_type: str, ret: float) -> bool:
    if verdict in _BULLISH:
        return ret >= 0 if subject_type == "holding" else ret > 0
    return ret < 0 if subject_type == "holding" else ret <= 0


def _format_table(title: str, summary: dict, horizons: tuple[int, ...]) -> str:
    header = f"  {'区分':<24}{'件数':>5}"
    for h in horizons:
        header += f" | {h}日 n{'':>2}avg{'':>3}win{'':>2}acc"
    lines = ["", f"■ {title}", header, "  " + "-" * (len(header) - 2)]
    for name in sorted(summary):
        s = summary[name]
        row = f"  {name:<24}{s['count']:>5}"
        for h in horizons:
            row += f" | {s[f'n_{h}']:>4} {_pct(s[f'avg_{h}']):>6} {_pct(s[f'win_{h}']):>5} {_pct(s[f'acc_{h}']):>5}"
        lines.append(row)
        if s["target"] or s["stop"] or s["split_suspect"]:
            lines.append(
                f"  {'':<24}      利確先着 {s['target']} / 損切り先着 {s['stop']}"
                f" / 分割疑いで除外 {s['split_suspect']}"
            )
    return "\n".join(lines)


def _pct(value) -> str:
    return "-" if value is None else f"{value:.1f}"
