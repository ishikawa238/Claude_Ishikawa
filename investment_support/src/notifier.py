"""
Slack 通知モジュール

朝通知・夜通知・月次レポートを Slack Incoming Webhook で送信する。
送信本文は report_log テーブルに保存する。
"""

import json
import math
from datetime import date, datetime

import requests

from src.config import CONFIG
from src.data_store import get_connection, insert_one
from src.logger import get_logger
from src.order_executor import generate_order_url

_EARNINGS_WARN_DAYS     = int(CONFIG["jquants"].get("earnings_alert_days_warning",  7))
_EARNINGS_CRITICAL_DAYS = int(CONFIG["jquants"].get("earnings_alert_days_critical", 3))

logger = get_logger(__name__)

_WEBHOOK_URL = CONFIG["slack"]["webhook_url"]

# モード → 絵文字
_MODE_EMOJI = {
    "normal":       "🟢",
    "caution":      "⚠️",
    "full_caution": "🔴",
}
_MODE_LABEL = {
    "normal":       "通常モード",
    "caution":      "警戒モード",
    "full_caution": "全面警戒モード",
}
_HOLDING_EMOJI = {
    "forced_stop": "🚨",
    "sl_near":     "⚠️",
    "sell_watch":  "🟡",
    "hold":        "🟢",
}


# ============================================================
# 朝通知
# ============================================================

def send_morning_report(
    target_date: date,
    market_env: dict,
    candidates: list[dict],
    holdings: list[dict],
) -> bool:
    """
    朝通知を生成して Slack に送信する。

    引数:
        target_date: 対象日
        market_env:  market_environment テーブルの行
        candidates:  エントリーセット付き買い候補（最大2件）
        holdings:    evaluate_holdings() の返り値
    """
    mode = market_env.get("mode", "caution")
    today_str = target_date.strftime("%Y-%m-%d (%a)")
    blocks = []

    # ヘッダー
    blocks.append(_section(f"📅 *朝の投資チェック｜{today_str}*"))
    blocks.append(_divider())

    # 強制損切り（最優先）
    forced = [h for h in holdings if h["label"] == "forced_stop"]
    if forced:
        blocks.append(_section(f"🚨 *【要即時対応】損切り対象（{len(forced)}件）*"))
        for h in forced:
            blocks.append(_holding_block(h, color="danger"))
        blocks.append(_divider())

    # 決算アラート
    earnings_alerts = _get_earnings_alerts(holdings)
    if earnings_alerts:
        blocks.append(_section("📅 *決算アラート*"))
        for h in earnings_alerts:
            blocks.append(_earnings_alert_block(h))
        blocks.append(_divider())

    # 市場環境
    blocks.append(_market_env_block(market_env))
    blocks.append(_divider())

    # 損切りライン接近
    sl_near = [h for h in holdings if h["label"] == "sl_near"]
    if sl_near:
        blocks.append(_section("⚠️ *損切りライン接近*"))
        for h in sl_near:
            blocks.append(_holding_block(h, color="warning"))
        blocks.append(_divider())

    # 買い候補（通常モードのみ）
    morning_candidates = candidates[:2]
    if mode == "normal" and morning_candidates:
        blocks.append(_section(f"🟢 *本日の買い候補（{len(morning_candidates)}件）*"))
        for es in morning_candidates:
            blocks.append(_entry_set_block(es))
        blocks.append(_divider())
    elif mode != "normal":
        blocks.append(_section(
            f"{_MODE_EMOJI[mode]} *{_MODE_LABEL[mode]}中 — 本日の新規買いは見送りを推奨します*"
        ))
        blocks.append(_divider())

    # 保有銘柄サマリー
    sell_w = [h for h in holdings if h["label"] == "sell_watch"]
    hold_n = [h for h in holdings if h["label"] == "hold"]
    blocks.append(_section(
        f"📋 *保有銘柄サマリー*\n"
        f"継続保有: {len(hold_n)}件 ｜ 売り検討: {len(sell_w)}件 ｜ "
        f"SL接近: {len(sl_near)}件 ｜ 強制損切り: {len(forced)}件"
    ))

    payload = {"blocks": blocks}
    return _send(payload, target_date, "morning")


# ============================================================
# 夜通知
# ============================================================

def send_evening_report(
    target_date: date,
    market_env: dict,
    entry_sets: list[dict],
    holdings: list[dict],
    failed_tickers_count: int = 0,
) -> bool:
    """
    夜通知を生成して Slack に送信する。

    引数:
        target_date:          対象日
        market_env:           market_environment テーブルの行
        entry_sets:           エントリーセット付き候補（最大3件）
        holdings:             evaluate_holdings() の返り値
        failed_tickers_count: データ取得失敗銘柄数
    """
    mode = market_env.get("mode", "caution")
    today_str = target_date.strftime("%Y-%m-%d (%a)")
    blocks = []

    # ヘッダー
    blocks.append(_section(f"🌙 *夜の投資レポート｜{today_str}*"))
    blocks.append(_divider())

    # 市場環境（詳細）
    blocks.append(_market_env_block(market_env, detail=True))
    blocks.append(_divider())

    # 買い候補（通常モードのみ）
    evening_sets = entry_sets[:3]
    if mode == "normal" and evening_sets:
        blocks.append(_section(f"🟢 *新規買い候補（{len(evening_sets)}件）*"))
        for i, es in enumerate(evening_sets, start=1):
            blocks.append(_section(f"━ No.{i} ━━━━━━━━━━━━━━━━━━━━━━━━"))
            blocks.append(_entry_set_block(es, show_conditions=True))
        blocks.append(_divider())
    elif mode != "normal":
        blocks.append(_section(
            f"{_MODE_EMOJI[mode]} *{_MODE_LABEL[mode]}中 — 新規買いシグナルは停止しています*\n"
            + _mode_detail(market_env)
        ))
        blocks.append(_divider())
    else:
        blocks.append(_section("本日は買い候補が検出されませんでした。"))
        blocks.append(_divider())

    # 決算アラート
    earnings_alerts = _get_earnings_alerts(holdings)
    if earnings_alerts:
        blocks.append(_section("📅 *決算アラート*"))
        for h in earnings_alerts:
            blocks.append(_earnings_alert_block(h))
        blocks.append(_divider())

    # 保有銘柄全件
    if holdings:
        blocks.append(_section(f"📋 *保有銘柄全件チェック（{len(holdings)}件）*"))
        # 優先度順: forced_stop > sl_near > sell_watch > hold
        priority = {"forced_stop": 0, "sl_near": 1, "sell_watch": 2, "hold": 3}
        for h in sorted(holdings, key=lambda x: priority.get(x["label"], 9)):
            blocks.append(_holding_block(h, detail=True))
        blocks.append(_divider())

    # 明日の注目ポイント
    tomorrow_notes = _build_tomorrow_notes(holdings)
    if tomorrow_notes:
        blocks.append(_section("🔭 *明日の注目ポイント*\n" + "\n".join(tomorrow_notes)))

    # データ取得失敗件数
    if failed_tickers_count > 0:
        blocks.append(_section(
            f"⚠️ データ取得失敗: {failed_tickers_count}件（logs/を確認してください）"
        ))

    payload = {"blocks": blocks}
    return _send(payload, target_date, "evening")


# ============================================================
# Block Kit ヘルパー
# ============================================================

def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _divider() -> dict:
    return {"type": "divider"}


def _market_env_block(env: dict, detail: bool = False) -> dict:
    mode  = env.get("mode", "caution")
    emoji = _MODE_EMOJI[mode]
    label = _MODE_LABEL[mode]

    n225_close = env.get("n225_close")
    n225_ma25  = env.get("n225_ma25")
    sp500      = env.get("sp500_close")
    sp500_ma25 = env.get("sp500_ma25")

    jp_str = _index_str("日経平均", n225_close, n225_ma25)
    us_str = _index_str("S&P500",   sp500,      sp500_ma25)

    if detail:
        text = f"{emoji} *市場環境確定値：{label}*\n{jp_str}\n{us_str}"
    else:
        text = f"{emoji} *市場環境：{label}*\n{jp_str}  /  {us_str}"

    return _section(text)


def _index_str(name: str, close, ma25) -> str:
    if close is None:
        return f"{name}: データなし"
    if ma25 is None:
        return f"{name}: {close:,.0f}"
    diff_pct = (close - ma25) / ma25 * 100
    arrow = "↑" if diff_pct >= 0 else "↓"
    return f"{name}: {close:,.0f}（MA25比 {diff_pct:+.1f}%{arrow}）"


def _mode_detail(env: dict) -> str:
    mode = env.get("mode", "caution")
    jp   = env.get("jp_above_ma25")
    us   = env.get("us_above_ma25")
    parts = []
    if jp == 0:
        parts.append(f"日経平均 {env.get('n225_close', '?'):,.0f} < MA25 {env.get('n225_ma25', '?'):,.0f}")
    if us == 0:
        parts.append(f"S&P500 {env.get('sp500_close', '?'):,.0f} < MA25 {env.get('sp500_ma25', '?'):,.0f}")
    return "\n".join(parts)


def _entry_set_block(es: dict, show_conditions: bool = False) -> dict:
    ticker  = es["ticker"]
    name    = es.get("name") or ticker
    market  = es["market"]
    pattern = es["pattern"]
    p_name  = "モメンタムブレイクアウト" if pattern == "A" else "押し目買い"

    warn_str = ""
    if es.get("_warn_narrow"):
        warn_str = "\n⚠️ 損切り幅が狭すぎます（ATRが小さい銘柄）"
    elif es.get("_warn_wide"):
        warn_str = "\n⚠️ 損切り幅が広めです（高ボラティリティ銘柄）"

    entry  = es["entry_price"]
    stop   = es["stop_loss_price"]
    target = es["target_price"]
    sl_pct = es["stop_loss_pct"]
    tg_pct = sl_pct * 2
    amount = es.get("recommended_amount")
    amount_str = f"{amount:,.0f}" if amount else "計算不可"
    currency   = "円" if market == "JP" else "USD"

    text = (
        f"*[{pattern}] {ticker} {name} ({market})*\n"
        f"パターン：{p_name}\n"
        f"エントリー目安：{entry:,.2f}{warn_str}\n"
        f"損切りライン：{stop:,.2f}（{sl_pct:.1f}%）\n"
        f"利確目安：{target:,.2f}（+{tg_pct:.1f}%）\n"
        f"推奨投入額：{amount_str} {currency}"
    )

    if show_conditions:
        try:
            conds = json.loads(es.get("conditions_met") or "[]")
            if conds:
                text += "\n" + "\n".join(conds)
        except (json.JSONDecodeError, TypeError):
            pass

    # 買い注文リンク
    buy_qty = _calc_buy_qty(amount, entry, market)
    if buy_qty and buy_qty > 0:
        url = generate_order_url(
            action="buy", ticker=ticker, market=market,
            qty=buy_qty, price=entry, stop=stop, name=name,
        )
        if url:
            text += f"\n📱 <{url}|ワンクリック買い注文（{buy_qty}株）>"

    return _section(text)


def _holding_block(h: dict, color: str = "", detail: bool = False) -> dict:
    label   = h["label"]
    emoji   = _HOLDING_EMOJI.get(label, "")
    ticker  = h["ticker"]
    name    = h.get("name") or ticker
    market  = h["market"]
    current = h["current_price"]
    pnl_pct = h["pnl_pct"]
    pnl_amt = h.get("pnl_amount")
    sl      = h["stop_loss_price"]
    qty     = h.get("quantity") or 0
    reasons = h.get("reasons") or []

    label_names = {
        "forced_stop": "強制損切り",
        "sl_near":     "SL接近",
        "sell_watch":  "売り検討",
        "hold":        "継続保有",
    }
    label_str = label_names.get(label, label)

    pnl_str = f"{pnl_pct:+.1f}%"
    if pnl_amt:
        currency = "円" if market == "JP" else "USD"
        pnl_str += f"（{pnl_amt:+,.0f}{currency}）"

    text = (
        f"{emoji} *{label_str}* | {ticker} {name} ({market})\n"
        f"終値: {current:,.2f} ｜ 損益: {pnl_str} ｜ SL: {sl:,.2f}"
    )

    if detail and reasons:
        text += "\n理由: " + " / ".join(reasons)

    # 売り注文リンク（強制損切り・売り検討のみ）
    if label in ("forced_stop", "sell_watch") and qty and qty > 0:
        url = generate_order_url(
            action="sell", ticker=ticker, market=market,
            qty=qty, price=current, name=name,
        )
        if url:
            text += f"\n📱 <{url}|ワンクリック売り注文（{qty}株）>"

    return _section(text)


def _get_earnings_alerts(holdings: list[dict]) -> list[dict]:
    """警告閾値以内に決算が迫っている保有銘柄を返す（日数昇順）。"""
    alerts = [
        h for h in holdings
        if h.get("earnings_days") is not None
        and h["earnings_days"] <= _EARNINGS_WARN_DAYS
    ]
    return sorted(alerts, key=lambda h: h["earnings_days"])


def _earnings_alert_block(h: dict) -> dict:
    """決算アラート1件分の Block Kit セクションを生成する。"""
    days     = h["earnings_days"]
    ann_date = h["earnings_date"]
    ticker   = h["ticker"]
    name     = h.get("name") or ticker
    qty      = h.get("quantity")
    pnl_amt  = h.get("pnl_amount")
    market   = h["market"]

    days_str = "本日" if days == 0 else f"{days}日後"
    emoji    = "🚨" if days <= _EARNINGS_CRITICAL_DAYS else "📅"

    pnl_str = ""
    if qty is not None and pnl_amt is not None:
        currency = "円" if market == "JP" else "USD"
        pnl_str  = f"\n現在保有中: {qty:.0f}株 / 含み損益: {pnl_amt:+,.0f}{currency}"
    elif qty is not None:
        pnl_str = f"\n現在保有中: {qty:.0f}株"

    text = f"{emoji} *{ticker} {name}* — {days_str}に決算発表予定（{ann_date}）{pnl_str}"
    return _section(text)


def _build_tomorrow_notes(holdings: list[dict]) -> list[str]:
    notes = []
    for h in holdings:
        if h["label"] == "forced_stop":
            notes.append(f"・{h['ticker']} {h.get('name','')}：損切りライン割込み → 本日寄付での手仕舞い推奨")
        elif h["label"] == "sl_near":
            notes.append(f"・{h['ticker']} {h.get('name','')}：損切りライン接近 → 引き続き注視")
        elif h["label"] == "sell_watch":
            notes.append(f"・{h['ticker']} {h.get('name','')}：売り検討シグナル → 判断要")
    return notes


def _calc_buy_qty(amount, entry_price, market: str) -> float | None:
    """推奨投入額とエントリー価格から買い数量を計算する。"""
    if not amount or not entry_price or entry_price <= 0:
        return None
    raw = amount / entry_price
    if market == "JP":
        # 日本株は100株単位
        lots = math.floor(raw / 100)
        return lots * 100 if lots > 0 else None
    else:
        # 米国株は端株対応（小数第2位まで）
        return math.floor(raw * 100) / 100 if raw >= 0.01 else None


# ============================================================
# 送信・保存
# ============================================================

def _send(payload: dict, target_date: date, report_type: str) -> bool:
    """
    Slack Webhook に POST して結果を report_log に保存する。
    返り値: 成功なら True
    """
    content = json.dumps(payload, ensure_ascii=False)

    # テスト用: Webhook URL が未設定の場合はコンソール出力のみ
    if not _WEBHOOK_URL or "XXXXX" in _WEBHOOK_URL:
        logger.warning("Slack Webhook URL が未設定です。コンソールに出力します。")
        _print_blocks(payload)
        _save_report_log(target_date, report_type, content, "skipped")
        return True

    try:
        resp = requests.post(
            _WEBHOOK_URL,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code == 200:
            logger.info(f"Slack 送信成功: {report_type}")
            _save_report_log(target_date, report_type, content, "success")
            return True
        else:
            logger.error(f"Slack 送信失敗: HTTP {resp.status_code} / {resp.text}")
            _save_report_log(target_date, report_type, content, "failed")
            return False
    except Exception as e:
        logger.error(f"Slack 送信エラー: {e}")
        _save_report_log(target_date, report_type, content, "failed")
        return False


def _save_report_log(
    target_date: date, report_type: str, content: str, status: str
) -> None:
    with get_connection() as conn:
        insert_one(conn, "report_log", {
            "report_date":  target_date.strftime("%Y-%m-%d"),
            "report_type":  report_type,
            "content":      content,
            "slack_status": status,
        })


def _print_blocks(payload: dict) -> None:
    """Webhook 未設定時にブロック内容をコンソールに出力する。"""
    import io, sys
    out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    out.write("\n" + "=" * 60 + "\n")
    for block in payload.get("blocks", []):
        if block.get("type") == "section":
            text = block.get("text", {}).get("text", "")
            out.write(text + "\n")
        elif block.get("type") == "divider":
            out.write("-" * 40 + "\n")
    out.write("=" * 60 + "\n\n")
    out.flush()
