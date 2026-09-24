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
from src.data_store import fetch_one, get_connection, insert_one
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
    "forced_stop":   "🚨",
    "sl_near":       "⚠️",
    "sell_watch":    "🟡",
    "hold":          "🟢",
    "tp_t1":         "🎯",
    "tp_t2":         "🎯",
    "tp_t3":         "🏆",
    "tp_trailing":   "📉",
    "tp_ma50_exit":  "🛑",
    "tp_full":       "🎯",
    "tp_partial":    "🎯",
    "tp_extend_hold": "📈",
    "tp_extend_exit": "📉",
    "stale_watch":   "⏳",
    "stale_exit":    "⏰",
}

def _strategy_badge() -> str:
    """現在の戦略を表すバッジ文字列を返す。"""
    return "📈 スイング"
_NEAR_MISS_COND_LABELS: dict[str, dict[str, str]] = {
    "A": {
        "cond_a_ma200_rising":  "MA200上昇中",
        "cond_a_ma_aligned":    "短期〜長期MA整列",
        "cond_a_52w_high_near": "52週高値更新圏",
        "cond_a_volume_surge":  "出来高急増",
        "cond_a_rsi_ok":        "RSI適正範囲",
        "cond_a_above_ma25":    "終値がMA25の上",
    },
    "B": {
        "cond_b_ma200_rising":  "MA200上昇中",
        "cond_b_ma_aligned":    "中期〜長期MA整列",
        "cond_b_pullback_touched": "MA25へのタッチ",
        "cond_b_bounced":       "MA25からの反発",
        "cond_b_ma25_rising":   "MA25上昇中",
        "cond_b_rsi_ok":        "RSI適正範囲",
    },
    "C": {
        "cond_c_weekly_uptrend": "EMA25>EMA75",
        "cond_c_rsi_oversold":   "RSI≤35",
        "cond_c_sar_reversal":   "MACD確認",
    },
}

# 条件チェックの短縮ラベル（インライン表示用）
_COND_SHORT_LABELS: dict[str, dict[str, str]] = {
    "A": {
        "cond_a_ma200_rising":  "MA200↑",
        "cond_a_ma_aligned":    "MA整列",
        "cond_a_52w_high_near": "52w高値",
        "cond_a_volume_surge":  "出来高",
        "cond_a_rsi_ok":        "RSI",
        "cond_a_above_ma25":    "MA25上",
    },
    "B": {
        "cond_b_ma200_rising":  "MA200↑",
        "cond_b_ma_aligned":    "MA整列",
        "cond_b_pullback_touched": "MA25タッチ",
        "cond_b_bounced":       "MA25反発",
        "cond_b_ma25_rising":   "MA25↑",
        "cond_b_rsi_ok":        "RSI",
    },
    "C": {
        "cond_c_weekly_uptrend": "週足MA25↑",
        "cond_c_at_support":     "サポート",
        "cond_c_rsi_oversold":   "RSI低",
        "cond_c_sar_reversal":   "SAR↑",
    },
}

_BATCH_TYPE_LABEL = {
    "evening_jp": "夜バッチ(JP)",
    "morning_all": "朝バッチ(全体)",
    "monthly": "月次バッチ",
    "watchlist_hourly": "お気に入り監視",
}


def _get_data_freshness() -> str:
    """batch_run_log から最新バッチの実行時刻を取得し、フッター用文字列を返す。"""
    try:
        with get_connection() as conn:
            row = fetch_one(
                conn,
                "SELECT batch_type, end_time FROM batch_run_log "
                "WHERE status IN ('success', 'partial_failure') "
                "ORDER BY end_time DESC LIMIT 1",
            )
        if row:
            label = _BATCH_TYPE_LABEL.get(row["batch_type"], row["batch_type"])
            return f"🕐 データ取得: {label} {row['end_time']}"
    except Exception:
        logger.debug("batch_run_log 読み込みスキップ")
    return ""


# ============================================================
# 朝通知
# ============================================================

def send_morning_report(
    target_date: date,
    market_env: dict,
    candidates: list[dict],
    holdings: list[dict],
    *,
    llm_feedback: list[dict] | None = None,
    near_misses: list[dict] | None = None,
    screening_stats: dict | None = None,
    committee: dict | None = None,
) -> bool:
    """
    朝通知を生成して Slack に送信する。

    引数:
        target_date:      対象日
        market_env:       market_environment テーブルの行
        candidates:       エントリーセット付き買い候補（最大2件）
        holdings:         evaluate_holdings() の返り値
        llm_feedback:     LLM アドバイザーからのフィードバック（任意）
        near_misses:      惜しかった銘柄（任意）
        screening_stats:  スクリーニング統計（screened, candidates, entry_sets 件数）
        committee:        投資委員会の判定（run_committee の返り値、任意）
    """
    mode = market_env.get("mode", "caution")
    today_str = target_date.strftime("%Y-%m-%d (%a)")
    blocks = []

    # ヘッダー + 市場環境（1ブロックに統合）
    env_emoji = _MODE_EMOJI[mode]
    env_label = _MODE_LABEL[mode]
    jp_str = _index_str("日経", market_env.get("n225_close"), market_env.get("n225_ma25"))
    us_str = _index_str("S&P500", market_env.get("sp500_close"), market_env.get("sp500_ma25"))
    blocks.append(_section(
        f"📅 *朝の投資チェック｜{today_str}* ｜ {env_emoji}{env_label} ｜ {_strategy_badge()}\n"
        f"{jp_str}  /  {us_str}"
    ))
    blocks.append(_divider())

    if screening_stats and screening_stats.get("alert"):
        blocks.append(_section(screening_stats["alert"]))
        blocks.append(_divider())

    # 強制損切り（最優先）
    forced = [h for h in holdings if h["label"] == "forced_stop"]
    if forced:
        blocks.append(_section(
            f"🚨 *【要即時対応】損切り対象（{len(forced)}件）*\n"
            "損切りラインを割り込んでいます。*成行売りでの手仕舞いを検討してください。*"
        ))
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

    # 損切りライン接近
    sl_near = [h for h in holdings if h["label"] == "sl_near"]
    if sl_near:
        blocks.append(_section("⚠️ *損切りライン接近*"))
        for h in sl_near:
            blocks.append(_holding_block(h, color="warning"))
        blocks.append(_divider())

    # 買い候補（市場環境にかかわらずスコア付きで表示）
    morning_candidates = candidates[:2]
    # データ不足情報（候補の有無にかかわらず表示）
    _data_insuf_note = ""
    if screening_stats and screening_stats.get("data_insufficient", 0) > 0:
        _data_insuf_note = f"\n⚠️ データ不足のため一部条件を判定保留: {screening_stats['data_insufficient']}銘柄"

    if morning_candidates:
        mode_note = ""
        if mode == "caution":
            mode_note = "（⚠️ 警戒モード — スコア減点あり）"
        elif mode == "full_caution":
            mode_note = "（🔴 全面警戒 — スコア大幅減点あり）"
        blocks.append(_section(
            f"📈 *本日の検討候補（{len(morning_candidates)}件）*{mode_note}"
            + _data_insuf_note
        ))
        for es in morning_candidates:
            blocks.append(_entry_set_block(es))
        blocks.extend(_near_miss_section(near_misses))
        blocks.append(_divider())
    elif mode != "normal":
        reason = _no_candidates_reason(screening_stats, near_misses, mode)
        blocks.append(_section(
            f"{_MODE_EMOJI[mode]} *{_MODE_LABEL[mode]}中のため、新規検討候補は停止しています*\n"
            "保有銘柄の損切り判定は通常通り実施しています。"
            + (f"\n\n{reason}" if reason else "")
        ))
        blocks.extend(_near_miss_section(near_misses))
        blocks.append(_divider())
    else:
        reason = _no_candidates_reason(screening_stats, near_misses, mode)
        blocks.append(_section(
            "本日はスコア基準を満たす検討候補が検出されませんでした。"
            + (f"\n\n{reason}" if reason else "")
        ))
        blocks.extend(_near_miss_section(near_misses))
        blocks.append(_divider())

    # 保有銘柄サマリー
    sell_w = [h for h in holdings if h["label"] == "sell_watch"]
    hold_n = [h for h in holdings if h["label"] == "hold"]
    blocks.append(_section(
        f"📋 *保有銘柄サマリー*\n"
        f"継続保有: {len(hold_n)}件 ｜ 売り検討: {len(sell_w)}件 ｜ "
        f"SL接近: {len(sl_near)}件 ｜ 強制損切り: {len(forced)}件"
    ))

    # 投資委員会 + LLM アドバイザー意見
    blocks.extend(_committee_blocks(committee))
    blocks.extend(_llm_feedback_blocks(llm_feedback))

    # フッター：データ鮮度 + 詳細レポートリンク
    freshness = _get_data_freshness()
    footer_text = f"📊 <{_REPORT_URL}|詳細レポート>"
    if freshness:
        footer_text += f" ｜ {freshness}"
    blocks.append(_section(footer_text))

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
    *,
    llm_feedback: list[dict] | None = None,
    near_misses: list[dict] | None = None,
    screening_stats: dict | None = None,
    price_source_alert: str | None = None,
    committee: dict | None = None,
) -> bool:
    """
    夜通知を生成して Slack に送信する。

    引数:
        target_date:          対象日
        market_env:           market_environment テーブルの行
        entry_sets:           エントリーセット付き候補（最大3件）
        holdings:             evaluate_holdings() の返り値
        failed_tickers_count: データ取得失敗銘柄数
        price_source_alert:   JP価格ソース障害時の警告文（任意）
        llm_feedback:         LLM アドバイザーからのフィードバック（任意）
        near_misses:          惜しかった銘柄（任意）
        screening_stats:      スクリーニング統計（screened, candidates, entry_sets 件数）
        committee:            投資委員会の判定（run_committee の返り値、任意）
    """
    mode = market_env.get("mode", "caution")
    today_str = target_date.strftime("%Y-%m-%d (%a)")
    blocks = []

    # ヘッダー + 市場環境（1ブロックに統合）
    env_emoji = _MODE_EMOJI[mode]
    env_label = _MODE_LABEL[mode]
    jp_str = _index_str("日経平均", market_env.get("n225_close"), market_env.get("n225_ma25"))
    us_str = _index_str("S&P500", market_env.get("sp500_close"), market_env.get("sp500_ma25"))
    header_text = (
        f"🌙 *夜の投資レポート｜{today_str}* ｜ {env_emoji}{env_label} ｜ {_strategy_badge()}\n"
        f"{jp_str}\n{us_str}"
    )
    mode_det = _mode_detail(market_env)
    if mode_det:
        header_text += f"\n{mode_det}"
    blocks.append(_section(header_text))
    blocks.append(_divider())

    if price_source_alert:
        blocks.append(_section(price_source_alert))
        blocks.append(_divider())

    if screening_stats and screening_stats.get("alert"):
        blocks.append(_section(screening_stats["alert"]))
        blocks.append(_divider())

    # 買い候補（市場環境にかかわらずスコア付きで表示）
    evening_sets = entry_sets[:3]
    # データ不足情報（候補の有無にかかわらず表示）
    _data_insuf_note = ""
    if screening_stats and screening_stats.get("data_insufficient", 0) > 0:
        _data_insuf_note = f"\n⚠️ データ不足のため一部条件を判定保留: {screening_stats['data_insufficient']}銘柄"

    if evening_sets:
        mode_note = ""
        if mode == "caution":
            mode_note = f"\n{_MODE_EMOJI[mode]} {_MODE_LABEL[mode]}中 — スコア減点あり"
        elif mode == "full_caution":
            mode_note = f"\n{_MODE_EMOJI[mode]} {_MODE_LABEL[mode]}中 — スコア大幅減点あり"
        blocks.append(_section(f"📈 *新規検討候補（{len(evening_sets)}件）*{mode_note}" + _data_insuf_note))
        for es in evening_sets:
            blocks.append(_entry_set_block(es, show_conditions=True))
        blocks.extend(_near_miss_section(near_misses))
        blocks.append(_divider())
    elif mode != "normal":
        reason = _no_candidates_reason(screening_stats, near_misses, mode)
        blocks.append(_section(
            f"{_MODE_EMOJI[mode]} *{_MODE_LABEL[mode]}中のため、新規検討候補は停止しています*\n"
            "保有銘柄の損切り判定は通常通り実施しています。\n"
            + _mode_detail(market_env)
            + (f"\n\n{reason}" if reason else "")
        ))
        blocks.extend(_near_miss_section(near_misses))
        blocks.append(_divider())
    else:
        reason = _no_candidates_reason(screening_stats, near_misses, mode)
        blocks.append(_section(
            "本日はスコア基準を満たす検討候補が検出されませんでした。"
            + (f"\n\n{reason}" if reason else "")
        ))
        blocks.extend(_near_miss_section(near_misses))
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

    # 投資委員会 + LLM アドバイザー意見
    blocks.extend(_committee_blocks(committee))
    blocks.extend(_llm_feedback_blocks(llm_feedback))

    # フッター：データ鮮度 + 詳細レポートリンク
    freshness = _get_data_freshness()
    footer_text = f"📊 <{_REPORT_URL}|詳細レポート>"
    if freshness:
        footer_text += f" ｜ {freshness}"
    blocks.append(_section(footer_text))

    payload = {"blocks": blocks}
    return _send(payload, target_date, "evening")


# ============================================================
# Block Kit ヘルパー
# ============================================================

# _section, _divider, _print_blocks は shared.slack から import 済み

_PROVIDER_DISPLAY = {"openai": "ChatGPT", "anthropic": "Claude", "gemini": "Gemini"}

_SIGNAL_EMOJI = {"good": "🟢", "caution": "⚠️", "neutral": "⚪"}


def _llm_feedback_blocks(feedback: list[dict] | None) -> list[dict]:
    """LLM フィードバックを Slack Block Kit セクションに変換する。"""
    if not feedback:
        return []

    valid = [fb for fb in feedback if not fb.get("error") and fb.get("text")]
    if not valid:
        return []

    blocks: list[dict] = []
    blocks.append(_divider())
    blocks.append(_section("🤖 *AIアドバイザー意見*"))

    for fb in valid:
        name = _PROVIDER_DISPLAY.get(fb["provider"], fb["provider"])
        model = fb.get("model", "")
        # 要約: 先頭3行のみ表示し、全文はダッシュボードに誘導
        full_text = fb["text"]
        lines = full_text.strip().split("\n")
        summary = "\n".join(lines[:3])
        if len(lines) > 3:
            summary += f"\n_… 続きは <{_REPORT_URL}|詳細レポート> で確認_"
        blocks.append(_section(f"*💬 {name}（{model}）:*\n{summary}"))

    return blocks


_VERDICT_EMOJI = {
    "BUY": "🟢", "WAIT": "🟡", "SKIP": "⚪",
    "HOLD": "🔵", "REDUCE": "🟠", "SELL": "🔴",
}
_MACRO_EMOJI = {"bullish": "🌤️ 追い風", "neutral": "☁️ 中立", "bearish": "🌧️ 逆風"}
_COMMITTEE_ROLE_NAMES = {
    "macro": "マクロ", "fundamental": "ファンダ", "technical": "テクニカル", "risk": "リスク",
}


def _confidence_bar(confidence: float | None) -> str:
    if confidence is None:
        return ""
    filled = round(confidence * 5)
    return f"{'■' * filled}{'□' * (5 - filled)} {confidence * 100:.0f}%"


def _committee_blocks(committee: dict | None) -> list[dict]:
    """投資委員会の判定を Slack Block Kit セクションに変換する（最大 3 ブロック）。"""
    if not committee or not committee.get("verdicts"):
        return []

    lines = ["🏛️ *AI投資委員会の審議結果*"]
    macro = committee.get("macro")
    if macro:
        lines.append(f"🌏 マクロ: {_MACRO_EMOJI.get(macro.get('stance'), macro.get('stance'))} — {macro.get('summary', '')[:80]}")

    for v in committee["verdicts"]:
        label = f"*{v['ticker']}{(' ' + v['name']) if v.get('name') else ''}*"
        kind = "保有" if v["subject_type"] == "holding" else "新規"
        if v["status"] == "skipped_budget":
            lines.append(f"\n⏸️ {label}（{kind}）本日の予算上限のため未審議")
            continue
        if v["status"] == "failed" or not v.get("verdict"):
            lines.append(f"\n⚠️ {label}（{kind}）審議不成立 — ルールベースの判定を参照してください")
            continue
        emoji = _VERDICT_EMOJI.get(v["verdict"], "❔")
        lines.append(f"\n{emoji} {label}（{kind}）→ *{v['verdict']}*  {_confidence_bar(v.get('confidence'))}")
        if v.get("rationale"):
            lines.append(f"　💡 {v['rationale'][:150]}")
        if v.get("dissent"):
            lines.append(f"　🗣️ 少数意見: {v['dissent'][:80]}")
        if v.get("absent"):
            names = "、".join(_COMMITTEE_ROLE_NAMES.get(a, a) for a in v["absent"])
            lines.append(f"　🪑 欠席: {names}")

    body = "\n".join(lines)
    if len(body) > 2900:
        body = body[:2900] + "…"
    return [
        _divider(),
        _section(body),
        _section(
            f"💰 今回の委員会コスト 約{committee.get('cost_jpy', 0):.1f}円 ｜ "
            "⚠️ 判定は検討材料であり投資助言ではありません"
        ),
    ]


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
    _PATTERN_DISPLAY_NAMES = {
        "A": "モメンタムブレイクアウト",
        "B": "押し目買い",
        "C": "サポートバウンス",
    }
    p_name  = _PATTERN_DISPLAY_NAMES.get(pattern, pattern)
    rs_rank = es.get("_rs_rank")
    rank_str = f" ランク{rs_rank}位" if rs_rank else ""
    rs_score = es.get("_rs_score")
    rs_str = f" RS:{rs_score:.0f}" if rs_score is not None else ""
    strategy = es.get("_strategy")
    _STRATEGY_LABELS = {
        "swing_jp": "押し目",
        "moomoo_swing": "押し目",
        "breakout_jp": "ブレイクアウト",
        "breakout_us": "ブレイクアウト",
    }
    strat_str = ""
    if strategy:
        label = _STRATEGY_LABELS.get(strategy, strategy)
        strat_str = f" [{label}]"

    entry  = es["entry_price"]
    stop   = es["stop_loss_price"]
    target = es["target_price"]
    sl_pct = es["stop_loss_pct"]
    tg_pct = sl_pct * 2
    amount = es.get("recommended_amount")
    amount_str = f"{amount:,.0f}" if amount else "計算不可"
    currency   = "円" if market == "JP" else "USD"
    qty_str = ""
    if amount and entry > 0:
        est_qty = amount / entry
        if est_qty >= 1:
            qty_str = f"（約{est_qty:.0f}株）"
        else:
            qty_str = f"（約{est_qty:.2f}株）"

    # RSI 適正範囲
    if pattern == "A":
        rsi_min = int(float(_SIG.get("pattern_a_rsi_min", 50)))
        rsi_max = int(float(_SIG.get("pattern_a_rsi_max", 70)))
    elif pattern == "C":
        rsi_min = 0
        rsi_max = int(float(_SIG.get("pattern_c_rsi_max", 35)))
    else:
        rsi_min = int(float(_SIG.get("pattern_b_rsi_min", 40)))
        rsi_max = int(float(_SIG.get("pattern_b_rsi_max", 60)))

    # スコア・判定種別
    final_score    = es.get("_final_score") or es.get("final_score")
    recommend_rank = es.get("_recommend_rank") or es.get("recommend_rank") or ""
    recommend_reason = es.get("_recommend_reason") or es.get("recommend_reason") or ""
    source = es.get("_candidate_source")
    met_count = es.get("_conditions_met_count")
    total_count = es.get("_conditions_total")

    # --- 行1: ヘッダー + パターン名 ---
    header = f"*[{pattern}] {ticker} {name} ({market}){rank_str}{rs_str}{strat_str}* — {p_name}"

    # --- 行2: スコア + 判定種別 + サブスコア（1行統合）---
    score_parts: list[str] = []
    if final_score is not None:
        fs = int(final_score)
        stars = "⭐" * (fs // 20) + "☆" * (5 - fs // 20)
        score_parts.append(f"🏅 {stars} *{final_score}点（{recommend_rank}ランク）*")
    if source == "strict":
        score_parts.append("✅全条件一致")
    elif source == "score":
        detail = f"（{met_count}/{total_count}）" if met_count is not None and total_count is not None else ""
        score_parts.append(f"⚠️スコア候補{detail}")
    sub_str = _sub_score_line(es)
    if sub_str:
        score_parts.append(sub_str)
    score_line = " ｜ ".join(score_parts) if score_parts else ""

    # --- 行3: 推奨理由（あれば）---
    reason_line = f"💡 {recommend_reason}" if recommend_reason else ""

    # --- 行4: エントリー → 損切 → 利確（1行統合）---
    price_line = (
        f"📍 エントリー: {entry:,.2f} → "
        f"損切: {stop:,.2f}（-{sl_pct:.1f}%） → "
        f"利確: {target:,.2f}（+{tg_pct:.1f}%） RR2:1"
    )

    # --- 行5: 推奨投入額 + テクニカル指標（1行統合）---
    rsi = es.get("_rsi14")
    vol = es.get("_vol_ratio")
    metrics_parts = [f"💰 推奨: {amount_str}{currency}{qty_str}"]
    if rsi:
        metrics_parts.append(f"RSI: {rsi:.0f}（{rsi_min}–{rsi_max}）")
    if vol:
        metrics_parts.append(f"出来高: {vol:.1f}倍")
    close = es.get("_close")
    high_52w = es.get("_high_52w")
    if close and high_52w and high_52w > 0:
        dist_pct = (high_52w - close) / close * 100
        if dist_pct <= 0:
            metrics_parts.append("52w高値: 更新中")
        else:
            metrics_parts.append(f"52w高値: {dist_pct:.1f}%")
    metrics_line = " ｜ ".join(metrics_parts)

    # --- 行6: MA整列 ---
    ma_line = _ma_alignment_line(es)

    # --- 行7: 警告（インライン統合）---
    warn_line = _build_warn_inline(es)

    # --- 行8: 条件チェック（夜通知のみ、インライン）---
    cond_line = ""
    if show_conditions:
        cond_line = _conditions_met_inline(es.get("conditions_met"))

    # 組み立て（空行は除外）
    lines = [l for l in [header, score_line, reason_line, price_line,
                          metrics_line, ma_line, warn_line, cond_line] if l]
    return _section("\n".join(lines))


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
    reasons = h.get("reasons") or []

    label_names = {
        "forced_stop":   "強制損切り",
        "sl_near":       "SL接近",
        "sell_watch":    "売り検討",
        "hold":          "継続保有",
        "tp_t1":         "利確検討(T1)",
        "tp_t2":         "利確検討(T2)",
        "tp_t3":         "最終利確(T3)",
        "tp_trailing":   "MA25割れ",
        "tp_ma50_exit":  "MA50割れ全撤退",
        "tp_full":       "利確（全売）",
        "tp_partial":    "利確（半分売却）",
        "tp_extend_hold": "利確到達・延長保有",
        "tp_extend_exit": "延長終了・全売",
        "stale_watch":   "長期停滞・売却検討",
        "stale_exit":    "長期停滞・全売推奨",
    }
    label_str = label_names.get(label, label)

    pnl_str = f"{pnl_pct:+.1f}%"
    if pnl_amt:
        currency = "円" if market == "JP" else "USD"
        pnl_str += f"（{pnl_amt:+,.0f}{currency}）"

    # 行1: ラベル + ティッカー + 終値 + 損益 + SL
    line1 = (
        f"{emoji} *{label_str}* ｜ {ticker} {name} ({market})"
        f" ｜ 終値: {current:,.2f} ｜ *{pnl_str}* ｜ SL: {sl:,.2f}"
    )

    # 行2: 保有日数 + ターゲット + SL引き上げ + テクニカル + 理由
    sub_parts: list[str] = []

    holding_days = h.get("holding_days")
    if holding_days is not None:
        sub_parts.append(f"保有: {holding_days}日")

    # 利確目標表示
    profit_type = h.get("profit_type")
    eff_pct = h.get("exit_effective_target_pct")
    eff_price = h.get("exit_effective_target_price")
    if eff_pct is not None and eff_price and current > 0:
        dist = (eff_price - current) / current * 100
        r_mult = h.get("exit_r_target_mult", 2.0)
        r_pct = h.get("exit_r_linked_target_pct")
        r_price = h.get("exit_r_linked_target_price")
        time_pct = h.get("exit_time_decay_target_pct")
        time_price = h.get("exit_time_decay_target_price")
        res_pct = h.get("exit_resistance_cap_pct")
        res_price = h.get("exit_resistance_cap_price")
        binding = h.get("exit_target_binding") or "r_linked"
        tp_parts = []
        if r_pct is not None and r_price:
            dist_r = (r_price - current) / current * 100
            mark = "→" if binding == "r_linked" else ""
            tp_parts.append(
                f"{mark}A:{r_mult:g}R +{r_pct:g}% {r_price:,.0f}({dist_r:+.1f}%)"
            )
        if h.get("exit_resistance_cap_active") and res_pct is not None and res_price:
            dist_b = (res_price - current) / current * 100
            mark = "→" if binding == "resistance" else ""
            tp_parts.append(f"{mark}B:+{res_pct:g}% {res_price:,.0f}({dist_b:+.1f}%)")
        if time_pct is not None and time_price:
            dist_a = (time_price - current) / current * 100
            tp_parts.append(f"参考:+{time_pct:g}% {time_price:,.0f}({dist_a:+.1f}%)")
        if tp_parts:
            sub_parts.append("利確: " + " / ".join(tp_parts))
        else:
            sub_parts.append(f"利確目標: +{eff_pct:g}%（{eff_price:,.2f} / あと{dist:+.1f}%）")
        if binding == "resistance" and h.get("exit_resistance_cap_active"):
            sub_parts.append("有効=Bレジキャップ")
        elif binding == "r_linked":
            sub_parts.append(f"有効=A{r_mult:g}R")
        if h.get("exit_extend_active"):
            sub_parts.append("延長保有中")
        sell_q = h.get("exit_sell_qty") or 0
        if sell_q > 0:
            sub_parts.append(f"推奨売却: {sell_q:.0f}株")
    elif profit_type in ("I", "II") and current > 0:
        tp_parts = []
        for stage, lbl in [("tp_t1", "T1"), ("tp_t2", "T2"), ("tp_t3", "T3")]:
            price_key = f"{stage.replace('tp_', '')}_price"
            reached_key = f"{stage.replace('tp_', '')}_reached"
            price = h.get(price_key)
            reached = h.get(reached_key)
            if price is None:
                if stage == "tp_t3" and profit_type == "II":
                    tp_parts.append(f"{lbl}: MA25トレーリング")
                continue
            if reached:
                tp_parts.append(f"✓{lbl} {price:,.0f}")
            else:
                dist = (price - current) / current * 100
                tp_parts.append(f"{lbl} {price:,.0f}({dist:+.1f}%)")
        if tp_parts:
            sub_parts.append(" / ".join(tp_parts))
    else:
        target_price = h.get("target_price")
        if target_price and current > 0:
            target_dist_pct = (target_price - current) / current * 100
            sub_parts.append(f"目標まで: {target_dist_pct:+.1f}%（{target_price:,.2f}）")

    # SL引き上げ推奨
    adj_stop = h.get("tp_adjusted_stop")
    if adj_stop and adj_stop > sl:
        sub_parts.append(f"SL↑: {sl:,.0f}→{adj_stop:,.0f}")

    if detail:
        tech_str = _holding_tech_inline(h)
        if tech_str:
            sub_parts.append(tech_str)
        if reasons:
            sub_parts.append("理由: " + " / ".join(reasons))

    line2 = "　 " + " ｜ ".join(sub_parts) if sub_parts else ""

    lines = [l for l in [line1, line2] if l]
    return _section("\n".join(lines))



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
    c = es.get("_pattern_c_score")
    l = es.get("_liquidity_score")
    if t is None and a is None and b is None and c is None and l is None:
        return ""
    parts = []
    if t is not None:
        parts.append(f"トレンド:{t}")
    if a is not None:
        parts.append(f"ブレイクアウト:{a}")
    if b is not None:
        parts.append(f"押し目:{b}")
    if c is not None:
        parts.append(f"サポート:{c}")
    if l is not None:
        parts.append(f"流動性:{l}")
    return "📈 " + " / ".join(parts)



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
    return f"📐 MA: {ma_str} ({label})"


def _build_warn_inline(es: dict) -> str:
    """警告フラグを ｜ 区切りで1行にまとめる。警告がなければ空文字。"""
    parts: list[str] = []
    if es.get("_skip_reason"):
        parts.append(f"🚫 {es['_skip_reason']}")
    elif es.get("_warn_narrow"):
        parts.append("⚠️ 損切り幅が狭い")
    elif es.get("_warn_wide"):
        parts.append("⚠️ 損切り幅が広め")
    if es.get("_warn_earnings"):
        parts.append(f"📅 {es['_warn_earnings']} — 注意")
    if es.get("_warn_at_capacity"):
        remaining = es.get("_remaining_slots", 0)
        if remaining <= 0:
            parts.append("🈵 保有枠満杯")
        else:
            parts.append(f"📦 残り{remaining}枠")
    if es.get("_warn_too_small"):
        parts.append("⚠️ 推奨額が新規エントリー下限未満（攻めモード）")
    return " ｜ ".join(parts)


def _add_buy_section(holdings: list[dict]) -> list[dict]:
    """
    攻めモード時に買い増し候補（add_buy_signal.eligible == True）を
    Slack ブロックとしてまとめて返す。該当銘柄がなければ空リスト。
    """
    if not is_offensive() or not holdings:
        return []

    eligible = [
        h for h in holdings
        if h.get("add_buy_signal") and h["add_buy_signal"].get("eligible")
    ]
    if not eligible:
        return []

    blocks: list[dict] = []
    blocks.append(_section(f"📈 *買い増し候補（{len(eligible)}件）*"))
    for h in eligible:
        ticker = h["ticker"]
        name = h.get("name") or ticker
        market = h.get("market", "")
        pnl_pct = h.get("pnl_pct", 0)
        current = h.get("current_price", 0)
        reasons = h["add_buy_signal"].get("reasons") or []
        line1 = (
            f"📈 *{ticker} {name} ({market})* "
            f"｜ 現値: {current:,.2f} ｜ 含み益: {pnl_pct:+.1f}%"
        )
        line2 = "　 " + " / ".join(reasons) if reasons else ""
        lines = [l for l in [line1, line2] if l]
        blocks.append(_section("\n".join(lines)))
    blocks.append(_divider())
    return blocks


def _conditions_met_inline(conditions_json: str | None) -> str:
    """conditions_met JSON を1行のインライン形式に変換する。"""
    try:
        conds = json.loads(conditions_json or "[]")
    except (json.JSONDecodeError, TypeError):
        return ""
    if not conds:
        return ""
    return " ".join(c.strip().replace(" ← 未達", "") for c in conds)


def _conditions_check_inline(data: dict, pattern: str) -> str:
    """条件チェックを ✅/❌ の短縮ラベルで1行にまとめる。"""
    labels = _COND_SHORT_LABELS.get(pattern, {})
    parts: list[str] = []
    for col, short in labels.items():
        val = data.get(col)
        if val == 1:
            parts.append(f"✅{short}")
        elif val == 0:
            parts.append(f"❌{short}")
    return " ".join(parts)


def _holding_tech_inline(h: dict) -> str:
    """保有銘柄のテクニカル情報を短縮形で返す。データがなければ空文字。"""
    ma5 = h.get("tech_ma5")
    ma25 = h.get("tech_ma25")
    rsi14 = h.get("tech_rsi14")
    parts: list[str] = []
    if ma5 is not None and ma25 is not None:
        arrow = "↑" if ma5 >= ma25 else "↓"
        parts.append(f"MA5{arrow}MA25")
    if rsi14 is not None:
        parts.append(f"RSI:{rsi14:.0f}")
    return " ".join(parts)


def _no_candidates_reason(
    screening_stats: dict | None,
    near_misses: list[dict] | None,
    mode: str,
) -> str:
    """買い候補が0件だった理由を診断して返す。"""
    lines: list[str] = []

    if screening_stats and screening_stats.get("alert"):
        lines.append(screening_stats["alert"])

    if mode != "normal":
        lines.append(f"→ {_MODE_LABEL[mode]}のため新規検討候補を停止中")
    elif screening_stats:
        screened = screening_stats.get("screened", 0)
        cand = screening_stats.get("candidates", 0)
        entry = screening_stats.get("entry_sets", 0)

        if screened == 0 and screening_stats.get("alert"):
            lines.append("→ Screening List は前回分の日付のままです（本日分は未更新）")
        else:
            lines.append(f"📊 スクリーニング通過: {screened:,}銘柄 → candidate: {cand}銘柄 → 通知: {entry}銘柄")

        if cand == 0 and screened > 0:
            lines.append("→ スコア50点以上の銘柄がありませんでした")
        elif entry == 0:
            lines.append(f"→ {cand}銘柄がcandidate判定されましたが、損切り幅・資金条件で全て見送り")

    # データ不足の銘柄数
    if screening_stats and screening_stats.get("data_insufficient", 0) > 0:
        insuf = screening_stats["data_insufficient"]
        lines.append(f"⚠️ データ不足のため一部条件を判定保留: {insuf}銘柄")

    # near_misses から最高スコアを抽出
    if near_misses:
        top = near_misses[0]
        top_score = int(top.get("final_score") or 0)
        top_ticker = top.get("ticker", "")
        top_name = top.get("name") or top_ticker
        lines.append(f"🏅 最高スコア: {top_score}点（{top_ticker} {top_name}）")

    return "\n".join(lines)


_NEAR_MISS_TYPE_LABEL = {
    "filtered":    "🚫 スコアは十分ですが損切り幅・資金条件で見送り",
    "demoted":     "⚠️ 警戒モードのためスコア減点で降格",
    "score_short": None,  # スコア不足は gap 表示で代替
}


def _near_miss_section(near_misses: list[dict] | None) -> list[dict]:
    """通知対象にならなかった注目銘柄を Slack Block Kit セクションのリストで返す。"""
    if not near_misses:
        return []

    blocks: list[dict] = []
    blocks.append(_section("👀 *惜しかった銘柄*"))

    for i, nm in enumerate(near_misses[:2], start=1):
        ticker = nm.get("ticker", "")
        name = nm.get("name") or ticker
        market = nm.get("market", "")
        pattern = nm.get("pattern") or "A"
        final_score = int(nm.get("final_score") or 0)
        nm_type = nm.get("near_miss_type", "score_short")

        _NM_PATTERN_NAMES = {
            "A": "モメンタムブレイクアウト",
            "B": "押し目買い",
            "C": "サポートバウンス",
        }
        p_name = _NM_PATTERN_NAMES.get(pattern, pattern)

        # 行1: ヘッダー + パターン
        line1 = f"*{i}. [{pattern}] {ticker} {name} ({market})* — {p_name}"

        # 行2: スコア + サブスコア（1行統合）
        type_label = _NEAR_MISS_TYPE_LABEL.get(nm_type)
        if nm_type == "score_short":
            gap = 50 - final_score
            score_part = f"*{final_score}点*（あと{gap}点）"
        else:
            score_part = f"*{final_score}点*"
            if type_label:
                score_part += f" {type_label}"

        sub_parts = []
        t = nm.get("trend_score")
        a = nm.get("pattern_a_score")
        b = nm.get("pattern_b_score")
        lq = nm.get("liquidity_score")
        if t is not None:
            sub_parts.append(f"トレンド:{int(t)}")
        if a is not None:
            sub_parts.append(f"ブレイクアウト:{int(a)}")
        if b is not None:
            sub_parts.append(f"押し目:{int(b)}")
        c = nm.get("pattern_c_score")
        if c is not None:
            sub_parts.append(f"サポート:{int(c)}")
        if lq is not None:
            sub_parts.append(f"流動性:{int(lq)}")
        sub_str = "📈 " + " / ".join(sub_parts) if sub_parts else ""
        line2 = f"{score_part} ｜ {sub_str}" if sub_str else score_part

        # 行3: 条件チェック（インライン）
        line3 = _conditions_check_inline(nm, pattern)

        # 行4: 理由（あれば）
        reason = nm.get("recommend_reason") or ""
        line4 = f"💡 {reason}" if reason else ""

        lines = [l for l in [line1, line2, line3, line4] if l]
        blocks.append(_section("\n".join(lines)))

    blocks.append(_section(
        "📋 <http://127.0.0.1:5100/screening?filter=watch|Screeningで監視銘柄の全件を見る>"
    ))
    blocks.append(_divider())
    return blocks


def _build_tomorrow_notes(holdings: list[dict]) -> list[str]:
    notes = []
    for h in holdings:
        if h["label"] == "forced_stop":
            notes.append(f"・{h['ticker']} {h.get('name','')}：損切りライン割込み → *翌営業日の成行売りを検討してください*")
        elif h["label"] == "sl_near":
            notes.append(f"・{h['ticker']} {h.get('name','')}：損切りライン接近 → 引き続き注視（割込み時は即手仕舞い）")
        elif h["label"] == "sell_watch":
            notes.append(f"・{h['ticker']} {h.get('name','')}：売り検討シグナル → 判断要")
    return notes


# ============================================================
# スキップ通知
# ============================================================

_WEEKDAY_JA = ("月", "火", "水", "木", "金", "土", "日")


def send_skip_notification(
    target_date: date,
    batch_type: str,
    reason: str,
) -> bool:
    """非取引日スキップ時に短い Slack 通知を送信する。"""
    batch_label = _BATCH_TYPE_LABEL.get(batch_type, batch_type)
    weekday_ja = _WEEKDAY_JA[target_date.weekday()]
    date_str = target_date.strftime("%Y-%m-%d")
    text = f"🌙 {date_str}（{weekday_ja}）{reason}、{batch_label}をスキップしました"
    payload = {"blocks": [_section(text)]}
    if batch_type == "watchlist_hourly":
        return False  # 毎時監視は市場クローズ時に通知しない
    report_type = "evening_skip" if "evening" in batch_type else "morning_skip"
    return _send(payload, target_date, report_type)


def send_watchlist_swing_alert(
    target_date: date,
    candidates: list[dict],
    *,
    market_label: str = "US",
) -> bool:
    """お気に入り監視: 厳格3条件を満たした銘柄の統計量を Slack 通知する。"""
    if not candidates:
        return False

    now_str = datetime.now().strftime("%H:%M")
    date_str = target_date.strftime("%Y-%m-%d")
    blocks: list[dict] = [
        _section(
            f"⭐ *お気に入り監視 — {market_label} スイング候補*\n"
            f"{date_str} {now_str} JST\n"
            f"_※ 日足未確定・参考値。③はヒスト反転+GC 両方必須_"
        ),
        _divider(),
    ]

    for c in candidates:
        name = c.get("name") or c.get("ticker")
        ticker = c.get("ticker")
        market = c.get("market", "")
        close = c.get("close")
        ema25 = c.get("ema25")
        ema75 = c.get("ema75")
        rsi = c.get("rsi14")
        atr = c.get("atr14")
        vol_ratio = c.get("vol_ratio")
        diff = c.get("macd_diff")
        dea = c.get("macd_dea")
        hist = c.get("macd_hist")
        hist_prev = c.get("macd_hist_prev")

        price_s = f"${close:.2f}" if market == "US" and close else (
            f"¥{close:,.0f}" if close else "—"
        )
        lines = [
            f"*{ticker}* — {name}",
            f"  現値: {price_s}",
            f"  ① EMA25: {_fmt_num(ema25)} / EMA75: {_fmt_num(ema75)} ✓",
            f"  ② RSI14: {_fmt_num(rsi)} ✓",
            f"  ③ MACD: DIF {_fmt_num(diff)} / DEA {_fmt_num(dea)} / Hist {_fmt_num(hist)}"
            + (f"（前日 {_fmt_num(hist_prev)}）" if hist_prev is not None else "")
            + " ✓ ヒスト反転+GC",
        ]
        if vol_ratio is not None:
            lines.append(f"  出来高比: {vol_ratio:.2f}x")
        if atr is not None:
            atr_s = f"${atr:.2f}" if market == "US" else f"¥{atr:,.0f}"
            lines.append(f"  ATR14: {atr_s}")
        blocks.append(_section("\n".join(lines)))

    blocks.append(_divider())
    blocks.append(_section(f"<{_REPORT_URL}|ダッシュボードを開く>"))
    payload = {"blocks": blocks}
    return _send(payload, target_date, "watchlist_alert")


def send_holdings_intraday_alert(
    target_date: date,
    holdings: list[dict],
) -> bool:
    """保有銘柄の損切り・利確到達を Slack 通知する。"""
    if not holdings:
        return False

    now_str = datetime.now().strftime("%H:%M")
    date_str = target_date.strftime("%Y-%m-%d")
    blocks: list[dict] = [
        _section(
            f"📌 *保有銘柄アラート*\n"
            f"{date_str} {now_str} JST\n"
            f"_※ 現値ベースの参考判定_"
        ),
        _divider(),
    ]

    for h in holdings:
        alert_type = h.get("alert_type")
        if alert_type == "stop_loss":
            prefix = "🚨 *損切りライン到達*"
        else:
            prefix = "🎯 *利確ターゲット到達*"
        block = _holding_block(h, detail=True)
        text = block["text"]["text"]
        block["text"]["text"] = f"{prefix}\n{text}"
        blocks.append(block)

    blocks.append(_divider())
    blocks.append(_section(f"<{_REPORT_URL}|ダッシュボードを開く>"))
    payload = {"blocks": blocks}
    return _send(payload, target_date, "watchlist_holding_alert")


def _fmt_num(value) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
        if abs(v) >= 100:
            return f"{v:,.2f}"
        return f"{v:.4g}"
    except (TypeError, ValueError):
        return "—"


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

