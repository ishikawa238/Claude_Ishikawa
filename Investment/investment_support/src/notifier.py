"""
Slack 通知モジュール

朝通知・夜通知・月次レポートを Slack Incoming Webhook で送信する。
送信本文は report_log テーブルに保存する。
"""

import json
from datetime import date, datetime

from shared.slack import (
    divider as _divider,
    print_blocks as _print_blocks,
    section as _section,
    send_webhook,
)

from src.config import CONFIG
from src.data_store import get_connection, insert_one
from src.logger import get_logger

_EARNINGS_WARN_DAYS     = int(CONFIG["jquants"].get("earnings_alert_days_warning",  7))
_EARNINGS_CRITICAL_DAYS = int(CONFIG["jquants"].get("earnings_alert_days_critical", 3))

logger = get_logger(__name__)

_WEBHOOK_URL = CONFIG["slack"]["webhook_url"]
_SIG = CONFIG["signal"]
_REPORT_URL = "http://127.0.0.1:5100/report"

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

    # 買い候補（市場環境にかかわらずスコア付きで表示）
    morning_candidates = candidates[:2]
    if morning_candidates:
        mode_note = ""
        if mode == "caution":
            mode_note = "（⚠️ 警戒モード — スコア減点あり）"
        elif mode == "full_caution":
            mode_note = "（🔴 全面警戒 — スコア大幅減点あり）"
        blocks.append(_section(
            f"📈 *本日の買い候補（{len(morning_candidates)}件）*{mode_note}"
        ))
        for es in morning_candidates:
            blocks.append(_entry_set_block(es))
        blocks.append(_divider())
    elif mode != "normal":
        blocks.append(_section(
            f"{_MODE_EMOJI[mode]} *{_MODE_LABEL[mode]}中 — 本日はスコア基準を満たす候補がありません*"
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

    # フッター：詳細レポートリンク
    blocks.append(_section(f"📊 <{_REPORT_URL}|詳細レポートを見る>"))

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

    # 買い候補（市場環境にかかわらずスコア付きで表示）
    evening_sets = entry_sets[:3]
    if evening_sets:
        mode_note = ""
        if mode == "caution":
            mode_note = f"\n{_MODE_EMOJI[mode]} {_MODE_LABEL[mode]}中 — スコア減点あり\n" + _mode_detail(market_env)
        elif mode == "full_caution":
            mode_note = f"\n{_MODE_EMOJI[mode]} {_MODE_LABEL[mode]}中 — スコア大幅減点あり\n" + _mode_detail(market_env)
        blocks.append(_section(f"📈 *新規買い候補（{len(evening_sets)}件）*{mode_note}"))
        for i, es in enumerate(evening_sets, start=1):
            blocks.append(_section(f"━ No.{i} ━━━━━━━━━━━━━━━━━━━━━━━━"))
            blocks.append(_entry_set_block(es, show_conditions=True))
        blocks.append(_divider())
    elif mode != "normal":
        blocks.append(_section(
            f"{_MODE_EMOJI[mode]} *{_MODE_LABEL[mode]}中 — スコア基準を満たす候補がありません*\n"
            + _mode_detail(market_env)
        ))
        blocks.append(_divider())
    else:
        blocks.append(_section("本日はスコア基準を満たす買い候補が検出されませんでした。"))
        blocks.append(_divider())

    # 決算アラート
    earnings_alerts = _get_earnings_alerts(holdings)
    if earnings_alerts:
        blocks.append(_section("📅 *決算アラート*"))
        for h in earnings_alerts:
            blocks.append(_earnings_alert_block(h))
        blocks.append(_divider())

    # ポートフォリオサマリー
    if holdings:
        summary_str = _portfolio_summary(holdings)
        if summary_str:
            blocks.append(_section(summary_str))
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

    # フッター：詳細レポートリンク
    blocks.append(_section(f"📊 <{_REPORT_URL}|詳細レポートを見る>"))

    payload = {"blocks": blocks}
    return _send(payload, target_date, "evening")


# ============================================================
# Block Kit ヘルパー
# ============================================================

# _section, _divider, _print_blocks は shared.slack から import 済み


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
    rs_rank = es.get("_rs_rank")
    rank_str = f"  ランク{rs_rank}位" if rs_rank else ""

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

    # RSI・出来高倍率
    rsi = es.get("_rsi14")
    vol = es.get("_vol_ratio")
    if pattern == "A":
        rsi_min = int(float(_SIG.get("pattern_a_rsi_min", 50)))
        rsi_max = int(float(_SIG.get("pattern_a_rsi_max", 70)))
    else:
        rsi_min = int(float(_SIG.get("pattern_b_rsi_min", 40)))
        rsi_max = int(float(_SIG.get("pattern_b_rsi_max", 60)))
    metrics_parts = []
    if rsi:
        metrics_parts.append(f"RSI: {rsi:.0f}（適正 {rsi_min}–{rsi_max}）")
    if vol:
        metrics_parts.append(f"出来高: {vol:.1f}倍（平均比）")
    metrics_str = "\n📊 " + "  ".join(metrics_parts) if metrics_parts else ""

    # スコア表示
    final_score    = es.get("_final_score") or es.get("final_score")
    recommend_rank = es.get("_recommend_rank") or es.get("recommend_rank") or ""
    recommend_reason = es.get("_recommend_reason") or es.get("recommend_reason") or ""
    score_str = ""
    if final_score is not None:
        score_str = f"\n🏅 おすすめ度：*{final_score}点（{recommend_rank}ランク）*"
        if recommend_reason:
            score_str += f"\n💡 {recommend_reason}"

    # サブスコア内訳
    sub_score_str = _sub_score_line(es)

    # 52週高値との距離
    high_52w_str = _high_52w_line(es)

    # MA整列の視覚表示
    ma_align_str = _ma_alignment_line(es)

    text = (
        f"*[{pattern}] {ticker} {name} ({market}){rank_str}*\n"
        f"パターン：{p_name}"
        f"{score_str}"
        f"{sub_score_str}\n"
        f"エントリー目安：{entry:,.2f}\n"
        f"損切りライン：{stop:,.2f}（-{sl_pct:.1f}%）→ 利確目安：{target:,.2f}（+{tg_pct:.1f}%）  リスクリワード2:1"
        f"{warn_str}\n"
        f"推奨投入額：{amount_str} {currency}"
        f"{metrics_str}"
        f"{high_52w_str}"
        f"{ma_align_str}"
    )

    if show_conditions:
        try:
            conds = json.loads(es.get("conditions_met") or "[]")
            if conds:
                text += "\n" + "\n".join(conds)
        except (json.JSONDecodeError, TypeError):
            pass

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

    # 保有日数・目標距離
    holding_days = h.get("holding_days")
    target_price = h.get("target_price")
    detail_parts = []
    if holding_days is not None:
        detail_parts.append(f"保有: {holding_days}日目")
    if target_price and current > 0:
        target_dist_pct = (target_price - current) / current * 100
        detail_parts.append(f"目標まで: {target_dist_pct:+.1f}%（利確目安: {target_price:,.2f}）")
    if detail_parts:
        text += "\n" + " ｜ ".join(detail_parts)

    if detail and reasons:
        text += "\n理由: " + " / ".join(reasons)

    # テクニカルスナップショット（detail のみ）
    if detail:
        tech_str = _holding_tech_line(h, current)
        text += tech_str

    return _section(text)


def _holding_tech_line(h: dict, current_price: float) -> str:
    """保有銘柄のテクニカル情報を1〜2行で返す。データがなければ空文字。"""
    ma5 = h.get("tech_ma5")
    ma25 = h.get("tech_ma25")
    rsi14 = h.get("tech_rsi14")

    parts = []
    # MA25 との位置関係
    if ma25 is not None and current_price > 0:
        ma25_diff_pct = (current_price - ma25) / ma25 * 100
        direction = "上方" if ma25_diff_pct >= 0 else "下方"
        rsi_part = f" ｜ RSI: {rsi14:.0f}" if rsi14 is not None else ""
        parts.append(f"📊 MA25: {ma25:,.2f}（{ma25_diff_pct:+.1f}%{direction}）{rsi_part}")

    # MA5 vs MA25 のトレンド状態
    if ma5 is not None and ma25 is not None:
        if ma5 >= ma25:
            parts.append("MA5↑MA25（上昇中）")
        else:
            parts.append("MA5↓MA25（下降中）")

    if not parts:
        return ""
    return "\n" + "\n".join(parts)


def _portfolio_summary(holdings: list[dict]) -> str:
    """ポートフォリオ全体のサマリー（含み損益合計・平均保有日数）を1行で返す。"""
    if not holdings:
        return ""

    # 含み損益合計（JP/US 別）
    pnl_jpy = 0.0
    pnl_usd = 0.0
    has_jpy = False
    has_usd = False
    days_list = []

    for h in holdings:
        amt = h.get("pnl_amount")
        market = h.get("market", "")
        if amt is not None:
            if market == "JP":
                pnl_jpy += amt
                has_jpy = True
            else:
                pnl_usd += amt
                has_usd = True
        d = h.get("holding_days")
        if d is not None:
            days_list.append(d)

    pnl_parts = []
    if has_jpy:
        pnl_parts.append(f"{pnl_jpy:+,.0f}円")
    if has_usd:
        pnl_parts.append(f"{pnl_usd:+,.0f}USD")
    pnl_str = " / ".join(pnl_parts) if pnl_parts else "算出不可"

    avg_days_str = ""
    if days_list:
        avg_days = sum(days_list) / len(days_list)
        avg_days_str = f" ｜ 平均保有日数: {avg_days:.0f}日"

    return f"💰 *ポートフォリオ: 含み損益合計 {pnl_str}{avg_days_str}*"


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


def _sub_score_line(es: dict) -> str:
    """サブスコア内訳を1行で返す。データがなければ空文字。"""
    t = es.get("_trend_score")
    a = es.get("_pattern_a_score")
    b = es.get("_pattern_b_score")
    l = es.get("_liquidity_score")
    if t is None and a is None and b is None and l is None:
        return ""
    parts = []
    if t is not None:
        parts.append(f"T:{t}")
    if a is not None:
        parts.append(f"A:{a}")
    if b is not None:
        parts.append(f"B:{b}")
    if l is not None:
        parts.append(f"L:{l}")
    return "\n📈 " + " / ".join(parts)


def _high_52w_line(es: dict) -> str:
    """52週高値との距離を表示する。データがなければ空文字。"""
    close = es.get("_close")
    high_52w = es.get("_high_52w")
    if not close or not high_52w or high_52w <= 0:
        return ""
    distance_pct = (high_52w - close) / close * 100
    if distance_pct <= 0:
        return "\n📍 52w高値: 更新中"
    return f"\n📍 52w高値まで: {distance_pct:.1f}%"


def _ma_alignment_line(es: dict) -> str:
    """MA整列の視覚表示を返す。データがなければ空文字。"""
    ma5 = es.get("_ma5")
    ma25 = es.get("_ma25")
    ma75 = es.get("_ma75")
    ma200 = es.get("_ma200")
    if ma25 is None or ma75 is None or ma200 is None:
        return ""
    # MA5 は None の場合がありうる
    parts = []
    if ma5 is not None:
        arrow_5_25 = "↑" if ma5 >= ma25 else "↓"
        parts.append(f"5{arrow_5_25}")
    arrow_25_75 = "↑" if ma25 >= ma75 else "↓"
    arrow_75_200 = "↑" if ma75 >= ma200 else "↓"
    parts.append(f"25{arrow_25_75}75{arrow_75_200}200")
    ma_str = "".join(parts)
    # 完全整列判定
    is_aligned = (
        (ma5 is None or ma5 >= ma25) and
        ma25 >= ma75 and
        ma75 >= ma200
    )
    label = "完全整列" if is_aligned else "部分整列"
    return f"\n📐 MA: {ma_str} ({label})"


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


# ============================================================
# 送信・保存
# ============================================================

def _send(payload: dict, target_date: date, report_type: str) -> bool:
    """
    Slack Webhook に POST して結果を report_log に保存する。
    返り値: 成功なら True
    """
    content = json.dumps(payload, ensure_ascii=False)
    success = send_webhook(_WEBHOOK_URL, payload, logger=logger)

    if not _WEBHOOK_URL or "XXXXX" in _WEBHOOK_URL:
        status = "skipped"
    elif success:
        logger.info(f"Slack 送信成功: {report_type}")
        status = "success"
    else:
        status = "failed"

    _save_report_log(target_date, report_type, content, status)
    return success


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

