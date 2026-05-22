"""
日次レポート Web サーバー (Phase 1-3 統合版)

Slack 通知のレポートリンクをクリック → ブラウザで保有銘柄・シグナル履歴・市場環境を確認する。

エンドポイント:
  /health              - ヘルスチェック
  /report              - メインダッシュボード
  /screening           - スクリーニング銘柄一覧
  /stock/<ticker>      - 個別銘柄詳細ページ
  /performance         - トレード成績分析ページ
  /settings            - 設定ページ（config.json の編集）
  /api/portfolio-pnl   - ポートフォリオ損益推移 JSON
  /api/score-radar     - スコアレーダー JSON
  /api/price-history   - 価格推移 JSON
  /api/market-timeline - 市場環境推移 JSON
  /api/perf-monthly    - 月別損益 JSON
  /api/perf-cumulative - 累積損益 JSON
  /api/sector-alloc    - セクター配分 JSON
  /api/screening-funnel - スクリーニングファネル統計 JSON
  /api/recent-signals  - 指定ティッカーの直近シグナル JSON（GET）
  /api/trade-result    - トレード結果記録（POST）
  /api/holdings/update-sl - 保有銘柄の損切りライン更新（POST）
  /api/settings        - 設定値取得（GET）/ 更新（POST）
  /api/batch/trigger   - バッチ手動実行（POST）
  /api/batch/status    - バッチ実行状態（GET）

実行方法（investment_support/ から）:
    python -m scripts.order_server
"""

import json
import math
import sys
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent  # Ishikawa238/ — shared パッケージの検索に必要
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

from flask import Flask, render_template, request, jsonify
from markupsafe import escape

from src.config import CONFIG
from src.data_store import fetch_all, fetch_one, get_connection, insert_one
from src.logger import get_logger

logger = get_logger(__name__)

_SERVER_CFG = CONFIG.get("report_server", {})
_HOST = _SERVER_CFG.get("host", "127.0.0.1")
_PORT = int(_SERVER_CFG.get("port", 5100))

# 見送り条件の閾値（entry_calculator と同じ設定を参照）
_SIG_CFG = CONFIG.get("signal", {})
_CAP_CFG = CONFIG.get("capital", {})
_SKIP_SL_MIN_PCT = float(_SIG_CFG.get("stop_loss_warning_min_pct", 1.0))
_SKIP_ATR_MULT = float(_SIG_CFG.get("atr_stop_multiplier", 2.0))
_SKIP_CAPITAL_JPY = float(_CAP_CFG.get("total_capital_jpy", 200000))
_SKIP_CAPITAL_USD = float(_CAP_CFG.get("total_capital_usd", 1500))
_SKIP_RISK_PCT = float(_CAP_CFG.get("max_risk_per_trade_pct", 2.0)) / 100
_SKIP_MAX_HOLDINGS = int(_CAP_CFG.get("max_concurrent_holdings", 3))

# --- USD/JPY 為替レート（1時間キャッシュ） ---
_fx_cache: dict[str, tuple[float, float]] = {}
_FX_CACHE_TTL = 3600


def _get_usdjpy() -> float:
    """USD/JPY レートを取得する（1時間キャッシュ付き）。"""
    now = _time.time()
    cached = _fx_cache.get("USDJPY")
    if cached and (now - cached[1]) < _FX_CACHE_TTL:
        return cached[0]

    from src.yfinance_fetcher import fetch_usdjpy_rate
    rate = fetch_usdjpy_rate()
    if rate and rate > 0:
        _fx_cache["USDJPY"] = (rate, now)
        return rate

    fallback = float(_CAP_CFG.get("usdjpy_fallback", 150.0))
    _fx_cache["USDJPY"] = (fallback, now)
    logger.warning(f"USD/JPY レート取得失敗。フォールバック値使用: {fallback}")
    return fallback


app = Flask(
    __name__,
    template_folder=str(_PROJECT_ROOT / "templates"),
    static_folder=str(_PROJECT_ROOT / "static"),
)

# ============================================================
# 定数
# ============================================================

_MODE_LABEL = {"normal": "通常モード", "caution": "警戒モード", "full_caution": "全面警戒モード"}
_MODE_CSS = {"normal": "mode-normal", "caution": "mode-caution", "full_caution": "mode-full_caution"}
_PATTERN_NAME = {"A": "モメンタムブレイクアウト", "B": "押し目買い", "C": "サポートバウンス"}
_RANK_CSS = {"S": "rank-s", "A": "rank-a", "B": "rank-b", "C": "rank-c", "Watch": "rank-watch"}
_LABEL_CSS = {"candidate": "label-candidate", "watch": "label-watch", "skip": "label-skip"}
_LABEL_DISPLAY = {"candidate": "候補", "watch": "監視", "skip": "スキップ"}
_PROFIT_TYPE_LABEL = {"I": "ブレイクアウト", "II": "トレンドフォロー", "III": "手動管理"}


def _build_tp_target_html(h: dict, cp: float) -> str:
    """保有銘柄の利確ターゲットをHTML文字列で構築する。"""
    from src.take_profit import _split_quantity

    profit_type = h.get("profit_type", "")
    type_label = _PROFIT_TYPE_LABEL.get(profit_type, "")
    r_value = float(h.get("r_value") or 0)
    entry = float(h.get("entry_price") or 0)
    qty = int(float(h.get("quantity") or 1)) or 1

    # 売却株数を計算
    t1_pct = float(h.get("t1_pct") or 50)
    t2_pct = float(h.get("t2_pct") or 30)
    t3_pct = float(h.get("t3_pct") or 20)
    t1_qty, t2_qty, t3_qty = _split_quantity(qty, t1_pct, t2_pct, t3_pct)

    # タイプ表示（小さく）
    parts = [f"<span class='tp-type-label'>{type_label}</span>"]

    stages = [
        ("t1", "T1", t1_qty),
        ("t2", "T2", t2_qty),
        ("t3", "T3", t3_qty),
    ]
    for stage, label, sell_qty in stages:
        price = h.get(f"{stage}_price")
        reached = h.get(f"{stage}_reached")

        # R倍率ラベル
        if price and r_value > 0 and entry > 0:
            r_mult = (price - entry) / r_value
            r_label = f"+{r_mult:.1f}R"
        elif stage == "t3" and profit_type == "II":
            r_label = "MA25割れ"
        else:
            r_label = ""

        # 売却株数テキスト
        qty_text = f" {sell_qty}株" if sell_qty > 0 else ""

        if price is None:
            # タイプII の T3: MA25トレーリング
            if stage == "t3" and profit_type == "II":
                if reached:
                    parts.append(f"<span class='tp-reached'>&#10003; {label} MA25割れ{qty_text}</span>")
                elif h.get("trailing_active"):
                    parts.append(f"<span class='tp-active'>&#8594; {label} MA25割れ{qty_text}</span>")
                else:
                    parts.append(f"<span class='tp-pending'>{label} MA25割れ{qty_text}</span>")
            continue

        if reached:
            parts.append(
                f"<span class='tp-reached'>&#10003; {label} {price:,.0f}"
                f" <span class='tp-sub'>({r_label}){qty_text}</span></span>"
            )
        elif cp >= price:
            parts.append(
                f"<span class='tp-active'>&#8594; {label} {price:,.0f}"
                f" <span class='tp-sub'>({r_label}){qty_text}</span></span>"
            )
        else:
            dist = (price - cp) / cp * 100
            parts.append(
                f"<span class='tp-pending'>{label} {price:,.0f}"
                f" <span class='tp-sub'>({r_label}) あと{dist:+.1f}%{qty_text}</span></span>"
            )
    return "<br>".join(parts) if len(parts) > 1 else "—"
_COND_A_LABELS = [
    ("cond_a_ma200_rising",  "MA200上昇"),
    ("cond_a_ma_aligned",    "MA整列"),
    ("cond_a_52w_high_near", "52週高値近傍"),
    ("cond_a_volume_surge",  "出来高急増"),
    ("cond_a_rsi_ok",        "RSI適正"),
    ("cond_a_above_ma25",    "MA25上方"),
]
_COND_B_LABELS = [
    ("cond_b_ma200_rising",     "MA200上昇"),
    ("cond_b_ma_aligned",       "MA整列"),
    ("cond_b_pullback_touched", "押し目タッチ"),
    ("cond_b_bounced",          "反発確認"),
    ("cond_b_ma25_rising",      "MA25上昇"),
    ("cond_b_rsi_ok",           "RSI適正"),
]
_COND_C_LABELS = [
    ("cond_c_weekly_uptrend", "週足MA25上昇"),
    ("cond_c_at_support",     "サポート近傍"),
    ("cond_c_rsi_oversold",   "RSI売られ過ぎ"),
    ("cond_c_sar_reversal",   "SAR反転"),
]


# ============================================================
# ルーティング: /health
# ============================================================

@app.route("/health")
def health():
    return "OK", 200


# ============================================================
# ルーティング: /report  (Phase 1 統合ダッシュボード)
# ============================================================

@app.route("/report")
def report():
    """メインダッシュボードページ"""
    today_str = date.today().strftime("%Y-%m-%d")

    with get_connection() as conn:
        market_env = fetch_one(
            conn,
            "SELECT * FROM market_environment ORDER BY date DESC LIMIT 1",
        )
        holdings = fetch_all(
            conn,
            """
            SELECT h.ticker, h.name, h.market, h.entry_price, h.entry_date,
                   h.quantity, h.stop_loss_price,
                   (SELECT adj_close FROM price_history
                    WHERE ticker = h.ticker AND date <= ? AND is_holiday = 0
                    ORDER BY date DESC LIMIT 1) AS current_price,
                   (SELECT target_price FROM trade_signal_log
                    WHERE ticker = h.ticker ORDER BY signal_date DESC LIMIT 1) AS target_price,
                   (SELECT pattern FROM trade_signal_log
                    WHERE ticker = h.ticker ORDER BY signal_date DESC LIMIT 1) AS signal_pattern,
                   t.rsi14, t.atr14, t.ma5, t.ma25, t.ma75, t.ma200,
                   d.final_score, d.recommend_rank,
                   d.trend_score, d.pattern_a_score, d.pattern_b_score, d.pattern_c_score, d.liquidity_score,
                   u.sector,
                   tp.profit_type, tp.r_value,
                   tp.t1_price, tp.t1_pct, tp.t1_reached,
                   tp.t2_price, tp.t2_pct, tp.t2_reached,
                   tp.t3_price, tp.t3_pct, tp.t3_reached,
                   tp.adjusted_stop, tp.trailing_active
            FROM holdings h
            LEFT JOIN technical_snapshot t
              ON t.ticker = h.ticker
              AND t.date = (SELECT MAX(date) FROM technical_snapshot WHERE ticker = h.ticker)
            LEFT JOIN daily_signals d
              ON d.ticker = h.ticker
              AND d.date = (SELECT MAX(date) FROM daily_signals WHERE ticker = h.ticker)
            LEFT JOIN universe_master u ON u.ticker = h.ticker
            LEFT JOIN take_profit_targets tp
              ON tp.ticker = h.ticker AND tp.is_active = 1
            WHERE h.is_closed = 0
            ORDER BY h.entry_date
            """,
            (today_str,),
        )
        candidates = fetch_all(
            conn,
            """
            SELECT ticker, name, market, pattern, entry_price, stop_loss_price,
                   stop_loss_pct, target_price, recommended_amount, signal_date
            FROM trade_signal_log
            WHERE signal_date >= date(?, '-7 days')
            ORDER BY signal_date DESC, ticker
            """,
            (today_str,),
        )
        # 市場環境タイムライン: 直近60日
        market_history = fetch_all(
            conn,
            """
            SELECT date, mode, n225_close, n225_ma25, sp500_close, sp500_ma25
            FROM market_environment
            WHERE date >= date(?, '-90 days')
            ORDER BY date
            """,
            (today_str,),
        )
        # LLM アドバイザーフィードバック: 最新日付分
        llm_feedback = fetch_all(
            conn,
            """
            SELECT provider, model, response_text, batch_type, feedback_date, created_at
            FROM llm_feedback_log
            WHERE feedback_date = (SELECT MAX(feedback_date) FROM llm_feedback_log)
              AND response_text != ''
            GROUP BY provider
            HAVING created_at = MAX(created_at)
            ORDER BY CASE provider WHEN 'openai' THEN 1 WHEN 'anthropic' THEN 2 WHEN 'gemini' THEN 3 END
            """,
        )
        # バッチ最終実行情報（B-3: データ鮮度）
        try:
            last_batch = fetch_one(
                conn,
                """
                SELECT batch_type, end_time, status
                FROM batch_run_log
                WHERE status IN ('success', 'partial_failure')
                ORDER BY end_time DESC LIMIT 1
                """,
            )
        except Exception:
            last_batch = None
        # データ不足銘柄数（B-2: 見送り理由）
        try:
            _latest_sig_date2 = "(SELECT MAX(date) FROM daily_signals)"
            data_insuf_row = fetch_one(
                conn,
                f"""
                SELECT COUNT(*) AS cnt FROM daily_signals
                WHERE date = {_latest_sig_date2}
                  AND (cond_a_ma200_rising IS NULL OR cond_b_ma200_rising IS NULL)
                """,
            )
            data_insufficient = data_insuf_row["cnt"] if data_insuf_row else 0
        except Exception:
            data_insufficient = 0

    # 最新ポートフォリオスナップショット
    try:
        from src.portfolio_snapshot import get_latest_snapshot
        latest_snapshot = get_latest_snapshot()
    except Exception:
        latest_snapshot = None

    return _render_report(today_str, market_env, holdings, candidates, market_history, llm_feedback, last_batch, data_insufficient, snapshot=latest_snapshot)


# ============================================================
# ルーティング: /stock/<ticker>  (Phase 2 個別銘柄)
# ============================================================

@app.route("/stock/<ticker>")
def stock_detail(ticker: str):
    """個別銘柄詳細ページ"""
    today_str = date.today().strftime("%Y-%m-%d")

    with get_connection() as conn:
        # 銘柄マスタ
        master = fetch_one(
            conn,
            "SELECT * FROM universe_master WHERE ticker = ?",
            (ticker,),
        )
        if not master:
            return f"<h3>銘柄 {escape(ticker)} が見つかりません</h3>", 404

        master = dict(master)

        # 最新テクニカル
        tech = fetch_one(
            conn,
            """SELECT * FROM technical_snapshot
               WHERE ticker = ? ORDER BY date DESC LIMIT 1""",
            (ticker,),
        )
        tech = dict(tech) if tech else {}

        # 最新価格
        price = fetch_one(
            conn,
            """SELECT adj_close, close, date FROM price_history
               WHERE ticker = ? AND is_holiday = 0
               ORDER BY date DESC LIMIT 1""",
            (ticker,),
        )
        price = dict(price) if price else {}

        # 前日価格
        prev_price = fetch_one(
            conn,
            """SELECT adj_close FROM price_history
               WHERE ticker = ? AND is_holiday = 0
               ORDER BY date DESC LIMIT 1 OFFSET 1""",
            (ticker,),
        )

        # 52週高値/安値
        range_52w = fetch_one(
            conn,
            """SELECT MAX(high) as high_52w, MIN(low) as low_52w
               FROM price_history
               WHERE ticker = ? AND date >= date(?, '-365 days')
                 AND is_holiday = 0""",
            (ticker, today_str),
        )
        range_52w = dict(range_52w) if range_52w else {}

        # スコア
        score = fetch_one(
            conn,
            """SELECT trend_score, pattern_a_score, pattern_b_score, pattern_c_score,
                      liquidity_score, final_score, recommend_rank, recommend_reason
               FROM daily_signals
               WHERE ticker = ? ORDER BY date DESC LIMIT 1""",
            (ticker,),
        )
        score = dict(score) if score else {}

        # シグナル履歴（全件）
        signals = fetch_all(
            conn,
            """SELECT signal_date, pattern, entry_price, stop_loss_price,
                      stop_loss_pct, target_price, recommended_amount, market_mode
               FROM trade_signal_log
               WHERE ticker = ?
               ORDER BY signal_date DESC""",
            (ticker,),
        )

        # ファンダメンタルズ（最新）
        fundamentals = fetch_one(
            conn,
            """SELECT * FROM fundamentals_snapshot
               WHERE ticker = ?
               ORDER BY fiscal_year DESC, fiscal_quarter DESC LIMIT 1""",
            (ticker,),
        )
        fundamentals = dict(fundamentals) if fundamentals else {}

        # リスク指標: キャッシュ確認
        risk_data = _get_or_calc_risk(conn, ticker, today_str, master.get("market", "JP"))

    return _render_stock_detail(
        ticker, master, tech, price, prev_price, range_52w,
        score, signals, fundamentals, risk_data,
    )


# ============================================================
# ルーティング: /performance  (Phase 2 パフォーマンス分析)
# ============================================================

@app.route("/performance")
def performance():
    """トレード成績分析ページ"""
    usdjpy = _get_usdjpy()
    with get_connection() as conn:
        # サマリー統計（JP/US別の pnl_amount 合計を含む）
        summary = fetch_one(
            conn,
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN trl.pnl_pct > 0 THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN trl.pnl_pct <= 0 THEN 1 ELSE 0 END) AS losses,
                      AVG(trl.pnl_pct) AS avg_pnl,
                      MIN(trl.pnl_pct) AS worst_trade,
                      MAX(trl.pnl_pct) AS best_trade,
                      SUM(CASE WHEN trl.pnl_pct > 0 THEN trl.pnl_pct ELSE 0 END) AS total_profit,
                      SUM(CASE WHEN trl.pnl_pct < 0 THEN ABS(trl.pnl_pct) ELSE 0 END) AS total_loss,
                      SUM(CASE WHEN trl.pnl_amount IS NOT NULL AND COALESCE(tsl.market,'JP')='JP'
                               THEN trl.pnl_amount ELSE 0 END) AS total_pnl_jpy,
                      SUM(CASE WHEN trl.pnl_amount IS NOT NULL AND tsl.market='US'
                               THEN trl.pnl_amount ELSE 0 END) AS total_pnl_usd
               FROM trade_result_log trl
               LEFT JOIN trade_signal_log tsl ON tsl.id = trl.signal_id
               WHERE trl.actual_exit_date IS NOT NULL""",
        )
        summary = dict(summary) if summary else {}

        # パターン別（JP/US別の pnl_amount 合計を含む）
        pattern_stats = fetch_all(
            conn,
            """SELECT tsl.pattern,
                      COUNT(*) AS total,
                      SUM(CASE WHEN trl.pnl_pct > 0 THEN 1 ELSE 0 END) AS wins,
                      AVG(trl.pnl_pct) AS avg_pnl,
                      SUM(CASE WHEN trl.pnl_amount IS NOT NULL AND COALESCE(tsl.market,'JP')='JP'
                               THEN trl.pnl_amount ELSE 0 END) AS sum_pnl_jpy,
                      SUM(CASE WHEN trl.pnl_amount IS NOT NULL AND tsl.market='US'
                               THEN trl.pnl_amount ELSE 0 END) AS sum_pnl_usd
               FROM trade_result_log trl
               LEFT JOIN trade_signal_log tsl ON tsl.id = trl.signal_id
               WHERE trl.actual_exit_date IS NOT NULL
               GROUP BY tsl.pattern""",
        )

        # 全トレード一覧（累積損益計算用）
        all_trades = fetch_all(
            conn,
            """SELECT trl.actual_exit_date, trl.pnl_pct, trl.pnl_amount,
                      trl.ticker, trl.exit_reason, tsl.pattern, tsl.market
               FROM trade_result_log trl
               LEFT JOIN trade_signal_log tsl ON tsl.id = trl.signal_id
               WHERE trl.actual_exit_date IS NOT NULL
               ORDER BY trl.actual_exit_date""",
        )

    return _render_performance(summary, pattern_stats, all_trades, usdjpy)


# ============================================================
# ルーティング: /screening  (スクリーニング銘柄一覧)
# ============================================================

@app.route("/screening")
def screening():
    """スクリーニング銘柄一覧ページ"""
    with get_connection() as conn:
        # JP/US それぞれの最新日付を取得（バッチ実行タイミングの差で日付がずれるため）
        market_dates = fetch_all(
            conn,
            "SELECT market, MAX(date) AS d FROM daily_signals GROUP BY market",
        )
        date_map = {dict(r)["market"]: dict(r)["d"] for r in market_dates}
        if not date_map:
            return "<h3>スクリーニングデータがありません</h3>", 404

        target_dates = sorted(set(date_map.values()))

        rows = fetch_all(
            conn,
            """SELECT d.ticker, u.name, u.sector, d.market, d.pattern, d.label,
                      d.cond_a_ma200_rising, d.cond_a_ma_aligned,
                      d.cond_a_52w_high_near, d.cond_a_volume_surge,
                      d.cond_a_rsi_ok, d.cond_a_above_ma25,
                      d.cond_b_ma200_rising, d.cond_b_ma_aligned,
                      d.cond_b_pullback_touched, d.cond_b_bounced,
                      d.cond_b_ma25_rising, d.cond_b_rsi_ok,
                      d.cond_c_weekly_uptrend, d.cond_c_at_support,
                      d.cond_c_rsi_oversold, d.cond_c_sar_reversal,
                      d.close, d.rsi14, d.atr14, d.vol_ratio, d.rs_rank,
                      d.final_score, d.recommend_rank, d.recommend_reason,
                      d.pattern_c_score
               FROM daily_signals d
               LEFT JOIN universe_master u ON u.ticker = d.ticker
               WHERE (d.market, d.date) IN (
                   SELECT market, MAX(date) FROM daily_signals GROUP BY market
               )
               ORDER BY d.final_score DESC""",
        )

    # 表示用日付文字列
    if len(target_dates) == 1:
        display_date = target_dates[0]
    else:
        parts = [f"{m}: {d}" for m, d in sorted(date_map.items())]
        display_date = " / ".join(parts)

    return _render_screening(display_date, [dict(r) for r in rows])


# ============================================================
# API エンドポイント群
# ============================================================

@app.route("/api/portfolio-pnl")
def api_portfolio_pnl():
    """ポートフォリオ損益推移（直近30営業日）"""
    today_str = date.today().strftime("%Y-%m-%d")

    with get_connection() as conn:
        holdings = fetch_all(
            conn,
            "SELECT ticker, entry_price, quantity, entry_date, market FROM holdings WHERE is_closed = 0",
        )
        if not holdings:
            return jsonify({"dates": [], "pnl": [], "pnl_pct": []})

        # 直近30営業日の日付を取得
        dates = fetch_all(
            conn,
            """SELECT DISTINCT date FROM price_history
               WHERE is_holiday = 0 AND date <= ?
               ORDER BY date DESC LIMIT 30""",
            (today_str,),
        )
        dates = [dict(d)["date"] for d in reversed(dates)]

        usdjpy = _get_usdjpy()
        pnl_list = []
        pnl_pct_list = []
        for d in dates:
            total_pnl = 0.0
            total_cost = 0.0
            for h in holdings:
                h = dict(h)
                if h["entry_date"] and h["entry_date"] > d:
                    continue
                p = fetch_one(
                    conn,
                    """SELECT adj_close FROM price_history
                       WHERE ticker = ? AND date <= ? AND is_holiday = 0
                       ORDER BY date DESC LIMIT 1""",
                    (h["ticker"], d),
                )
                if p:
                    cp = float(dict(p)["adj_close"])
                    ep = float(h["entry_price"])
                    qty = float(h["quantity"] or 1)
                    fx = usdjpy if h.get("market", "JP") != "JP" else 1.0
                    total_pnl += (cp - ep) * qty * fx
                    total_cost += ep * qty * fx
            pnl_list.append(round(total_pnl))
            pnl_pct_list.append(round(total_pnl / total_cost * 100, 2) if total_cost > 0 else 0)

    return jsonify({"dates": dates, "pnl": pnl_list, "pnl_pct": pnl_pct_list})


@app.route("/api/score-radar")
def api_score_radar():
    """スコアレーダーチャート用データ"""
    ticker = request.args.get("ticker")
    if not ticker:
        return jsonify({"error": "ticker parameter required"}), 400

    with get_connection() as conn:
        row = fetch_one(
            conn,
            """SELECT trend_score, pattern_a_score, pattern_b_score,
                      pattern_c_score, liquidity_score
               FROM daily_signals WHERE ticker = ?
               ORDER BY date DESC LIMIT 1""",
            (ticker,),
        )

    if not row:
        return jsonify({"trend": 0, "pattern_a": 0, "pattern_b": 0, "pattern_c": 0, "liquidity": 0})

    row = dict(row)
    return jsonify({
        "trend": row.get("trend_score") or 0,
        "pattern_a": row.get("pattern_a_score") or 0,
        "pattern_b": row.get("pattern_b_score") or 0,
        "pattern_c": row.get("pattern_c_score") or 0,
        "liquidity": row.get("liquidity_score") or 0,
    })


@app.route("/api/price-history")
def api_price_history():
    """価格推移データ"""
    ticker = request.args.get("ticker")
    days = int(request.args.get("days", 60))
    if not ticker:
        return jsonify({"error": "ticker parameter required"}), 400

    with get_connection() as conn:
        rows = fetch_all(
            conn,
            """SELECT ph.date, ph.adj_close AS close,
                      ts.ma25, ts.ma75
               FROM price_history ph
               LEFT JOIN technical_snapshot ts
                 ON ts.ticker = ph.ticker AND ts.date = ph.date
               WHERE ph.ticker = ? AND ph.is_holiday = 0
               ORDER BY ph.date DESC LIMIT ?""",
            (ticker, days),
        )

    rows = [dict(r) for r in reversed(rows)]
    return jsonify({
        "dates": [r["date"] for r in rows],
        "close": [r["close"] for r in rows],
        "ma25": [r["ma25"] for r in rows],
        "ma75": [r["ma75"] for r in rows],
    })


@app.route("/api/market-timeline")
def api_market_timeline():
    """市場環境推移データ"""
    days = int(request.args.get("days", 60))
    today_str = date.today().strftime("%Y-%m-%d")

    with get_connection() as conn:
        rows = fetch_all(
            conn,
            """SELECT date, mode, n225_close, n225_ma25, sp500_close, sp500_ma25
               FROM market_environment
               ORDER BY date DESC LIMIT ?""",
            (days,),
        )

    rows = [dict(r) for r in reversed(rows)]
    return jsonify({
        "dates": [r["date"] for r in rows],
        "n225": [r["n225_close"] for r in rows],
        "n225_ma25": [r["n225_ma25"] for r in rows],
        "sp500": [r["sp500_close"] for r in rows],
        "sp500_ma25": [r["sp500_ma25"] for r in rows],
        "modes": [r["mode"] for r in rows],
    })


@app.route("/api/perf-monthly")
def api_perf_monthly():
    """月別損益データ"""
    usdjpy = _get_usdjpy()
    with get_connection() as conn:
        rows = fetch_all(
            conn,
            """SELECT strftime('%Y-%m', trl.actual_exit_date) AS month,
                      SUM(trl.pnl_pct) AS total_pnl,
                      COUNT(*) AS trades,
                      SUM(CASE WHEN trl.pnl_pct > 0 THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN trl.pnl_amount IS NOT NULL AND COALESCE(tsl.market,'JP')='JP'
                               THEN trl.pnl_amount ELSE 0 END) AS pnl_jpy,
                      SUM(CASE WHEN trl.pnl_amount IS NOT NULL AND tsl.market='US'
                               THEN trl.pnl_amount ELSE 0 END) AS pnl_usd
               FROM trade_result_log trl
               LEFT JOIN trade_signal_log tsl ON tsl.id = trl.signal_id
               WHERE trl.actual_exit_date IS NOT NULL
               GROUP BY month ORDER BY month""",
        )

    rows = [dict(r) for r in rows]
    return jsonify({
        "months": [r["month"] for r in rows],
        "pnl": [round(r["total_pnl"] or 0, 2) for r in rows],
        "pnl_jpy": [round((r["pnl_jpy"] or 0) + (r["pnl_usd"] or 0) * usdjpy, 0)
                     for r in rows],
        "trades": [r["trades"] for r in rows],
        "wins": [r["wins"] or 0 for r in rows],
    })


@app.route("/api/perf-cumulative")
def api_perf_cumulative():
    """累積損益データ"""
    usdjpy = _get_usdjpy()
    with get_connection() as conn:
        rows = fetch_all(
            conn,
            """SELECT trl.actual_exit_date, trl.pnl_pct, trl.pnl_amount, tsl.market
               FROM trade_result_log trl
               LEFT JOIN trade_signal_log tsl ON tsl.id = trl.signal_id
               WHERE trl.actual_exit_date IS NOT NULL
               ORDER BY trl.actual_exit_date""",
        )

    dates = []
    cum = []
    cum_jpy = []
    running = 0.0
    running_jpy = 0.0
    for r in rows:
        r = dict(r)
        running += float(r["pnl_pct"] or 0)
        dates.append(r["actual_exit_date"])
        cum.append(round(running, 2))
        amt = float(r["pnl_amount"] or 0)
        if r.get("market") == "US":
            amt *= usdjpy
        running_jpy += amt
        cum_jpy.append(round(running_jpy, 0))

    return jsonify({"dates": dates, "cumulative_pnl": cum, "cumulative_pnl_jpy": cum_jpy})


@app.route("/api/recent-signals")
def api_recent_signals():
    """指定ティッカーの直近シグナル5件を返す（フォーム用）"""
    ticker = request.args.get("ticker", "").strip().upper()
    if not ticker:
        return jsonify([])

    with get_connection() as conn:
        rows = fetch_all(
            conn,
            """SELECT id, signal_date, pattern, entry_price, stop_loss_price,
                      target_price, market
               FROM trade_signal_log
               WHERE ticker = ?
               ORDER BY signal_date DESC LIMIT 5""",
            (ticker,),
        )
    return jsonify([dict(r) for r in rows])


@app.route("/api/trade-result", methods=["POST"])
def api_trade_result():
    """トレード結果を記録する（ダッシュボードフォームから呼び出し）"""
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "無効なリクエスト"}), 400

    ticker = (data.get("ticker") or "").strip().upper()
    exit_price_raw = data.get("actual_exit_price")
    exit_date = (data.get("actual_exit_date") or "").strip()
    exit_reason = data.get("exit_reason", "manual")
    signal_id = data.get("signal_id")
    actual_entry_price = data.get("actual_entry_price")
    actual_entry_date = data.get("actual_entry_date", "")
    memo = data.get("memo", "")

    # バリデーション
    if not ticker:
        return jsonify({"error": "ティッカーを入力してください"}), 400
    try:
        exit_price = float(exit_price_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "決済価格が不正です"}), 400
    if not exit_date:
        return jsonify({"error": "決済日を入力してください"}), 400
    if exit_reason not in ("stop_loss", "take_profit", "manual", "forced_stop"):
        exit_reason = "manual"

    # signal_id の処理
    if signal_id:
        signal_id = int(signal_id)
    else:
        # シグナルなし → 手動エントリーとして trade_signal_log に仮シグナル作成
        entry_p = float(actual_entry_price) if actual_entry_price else exit_price
        entry_d = actual_entry_date or exit_date
        sig_row = {
            "signal_date": entry_d,
            "ticker": ticker,
            "name": "",
            "market": "JP" if ticker.isdigit() else "US",
            "pattern": "A",
            "entry_price": entry_p,
            "stop_loss_price": round(entry_p * 0.92, 2),
            "stop_loss_pct": -8.0,
            "target_price": round(entry_p * 1.16, 2),
            "recommended_amount": None,
            "market_mode": "normal",
            "conditions_met": "manual_entry",
        }
        with get_connection() as conn:
            signal_id = insert_one(conn, "trade_signal_log", sig_row)

    # P&L 計算
    ref_entry = float(actual_entry_price) if actual_entry_price else None
    if ref_entry is None:
        with get_connection() as conn:
            sig = fetch_one(conn, "SELECT entry_price FROM trade_signal_log WHERE id=?",
                            (signal_id,))
        ref_entry = float(sig["entry_price"]) if sig else None

    pnl_pct = None
    pnl_amount = None
    if ref_entry and ref_entry > 0:
        pnl_pct = round((exit_price - ref_entry) / ref_entry * 100, 2)

    # holdings から数量を取得して pnl_amount を計算
    with get_connection() as conn:
        holding = fetch_one(
            conn,
            "SELECT id, quantity FROM holdings WHERE ticker=? AND is_closed=0",
            (ticker,),
        )
    if holding and holding.get("quantity") and ref_entry:
        pnl_amount = round((exit_price - ref_entry) * float(holding["quantity"]), 2)

    # INSERT
    row = {
        "signal_id": signal_id,
        "ticker": ticker,
        "actual_entry_price": float(actual_entry_price) if actual_entry_price else None,
        "actual_entry_date": actual_entry_date or None,
        "actual_exit_price": exit_price,
        "actual_exit_date": exit_date,
        "exit_reason": exit_reason,
        "pnl_pct": pnl_pct,
        "pnl_amount": pnl_amount,
        "memo": memo or None,
    }
    with get_connection() as conn:
        new_id = insert_one(conn, "trade_result_log", row)

    # holdings も決済済みに更新
    if holding:
        with get_connection() as conn:
            conn.execute(
                "UPDATE holdings SET is_closed=1, close_price=?, close_date=?, "
                "close_reason=?, updated_at=? WHERE id=?",
                (exit_price, exit_date, exit_reason,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"), holding["id"]),
            )

    sign = "+" if (pnl_pct or 0) >= 0 else ""
    return jsonify({
        "success": True,
        "id": new_id,
        "ticker": ticker,
        "pnl_pct": pnl_pct,
        "message": f"{ticker} のトレード結果を記録しました（{sign}{pnl_pct}%）",
    })


@app.route("/api/holdings/update-sl", methods=["POST"])
def api_holdings_update_sl():
    """保有銘柄の損切りラインを更新する（DB + holdings.csv 同期）"""
    import csv

    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "無効なリクエスト"}), 400

    ticker = (data.get("ticker") or "").strip()
    sl_raw = data.get("stop_loss_price")

    if not ticker:
        return jsonify({"error": "ティッカーを指定してください"}), 400
    try:
        new_sl = float(sl_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "損切り価格が不正です"}), 400
    if new_sl <= 0:
        return jsonify({"error": "損切り価格は0より大きい値にしてください"}), 400

    # DB 更新
    with get_connection() as conn:
        holding = fetch_one(
            conn,
            "SELECT id FROM holdings WHERE ticker=? AND is_closed=0",
            (ticker,),
        )
        if not holding:
            return jsonify({"error": f"{ticker} は保有銘柄に見つかりません"}), 404
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE holdings SET stop_loss_price=?, updated_at=? "
            "WHERE ticker=? AND is_closed=0",
            (new_sl, now, ticker),
        )

    # holdings.csv も同期更新
    csv_path = _PROJECT_ROOT / "holdings.csv"
    if csv_path.exists():
        rows = []
        fieldnames = None
        with open(csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            for row in reader:
                if row.get("ticker", "").strip() == ticker:
                    row["stop_loss_price"] = str(new_sl)
                rows.append(row)
        if fieldnames:
            with open(csv_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)

    logger.info(f"SL更新: {ticker} → {new_sl}")
    return jsonify({
        "success": True,
        "ticker": ticker,
        "stop_loss_price": new_sl,
    })


@app.route("/api/sector-alloc")
def api_sector_alloc():
    """セクター配分データ（円換算）"""
    with get_connection() as conn:
        rows = fetch_all(
            conn,
            """SELECT COALESCE(u.sector, '不明') AS sector,
                      h.market,
                      SUM(h.entry_price * h.quantity) AS total_value
               FROM holdings h
               LEFT JOIN universe_master u ON u.ticker = h.ticker
               WHERE h.is_closed = 0
               GROUP BY sector, h.market
               ORDER BY total_value DESC""",
        )

    usdjpy = _get_usdjpy()
    sector_map: dict[str, float] = {}
    for r in rows:
        r = dict(r)
        fx = usdjpy if r.get("market", "JP") != "JP" else 1.0
        sec = r["sector"]
        sector_map[sec] = sector_map.get(sec, 0) + (r["total_value"] or 0) * fx

    sectors = sorted(sector_map.keys(), key=lambda s: sector_map[s], reverse=True)
    return jsonify({
        "sectors": sectors,
        "values": [round(sector_map[s]) for s in sectors],
    })


@app.route("/api/portfolio-risk")
def api_portfolio_risk():
    """ポートフォリオ全体のリスク指標（加重平均）"""
    today_str = date.today().isoformat()
    usdjpy = _get_usdjpy()

    with get_connection() as conn:
        holdings = fetch_all(
            conn,
            """SELECT h.ticker, h.market, h.entry_price, h.quantity,
                      ph.adj_close AS current_price
               FROM holdings h
               LEFT JOIN (
                   SELECT ticker, adj_close FROM price_history
                   WHERE (ticker, date) IN (
                       SELECT ticker, MAX(date) FROM price_history GROUP BY ticker
                   )
               ) ph ON ph.ticker = h.ticker
               WHERE h.is_closed = 0""",
        )
        if not holdings:
            return jsonify({"var_99_1d": None, "beta": None, "sharpe": None,
                            "total_positions": 0})

        total_value = 0.0
        weighted_var = 0.0
        weighted_beta = 0.0
        weighted_sharpe = 0.0
        valid_count = 0

        for h in holdings:
            h = dict(h)
            cp = float(h.get("current_price") or h.get("entry_price") or 0)
            qty = float(h.get("quantity") or 1)
            market = h.get("market", "JP")
            fx = usdjpy if market != "JP" else 1.0
            pos_val = cp * qty * fx

            risk = _get_or_calc_risk(conn, h["ticker"], today_str, market)
            var_val = risk.get("var_99_1d")
            beta_val = risk.get("beta_60d")
            sharpe_val = risk.get("sharpe_60d")

            if var_val is not None:
                weighted_var += abs(var_val) * pos_val
                total_value += pos_val
                valid_count += 1
            if beta_val is not None:
                weighted_beta += beta_val * pos_val
            if sharpe_val is not None:
                weighted_sharpe += sharpe_val * pos_val

    if total_value > 0:
        avg_var = round(weighted_var / total_value * 100, 2)
        avg_beta = round(weighted_beta / total_value, 2)
        avg_sharpe = round(weighted_sharpe / total_value, 2)
    else:
        avg_var = avg_beta = avg_sharpe = None

    return jsonify({
        "var_99_1d": avg_var,
        "beta": avg_beta,
        "sharpe": avg_sharpe,
        "total_positions": valid_count,
    })


@app.route("/api/system-health")
def api_system_health():
    """システムヘルス情報（最終バッチ・エラー数・データ鮮度）"""
    with get_connection() as conn:
        last_batch = fetch_one(
            conn,
            """SELECT batch_type, status, end_time
               FROM batch_run_log ORDER BY end_time DESC LIMIT 1""",
        )
        error_count = fetch_one(
            conn,
            """SELECT COUNT(*) AS cnt FROM error_log
               WHERE created_at >= datetime('now', '-24 hours')""",
        )
        latest_price = fetch_one(
            conn,
            "SELECT MAX(date) AS d FROM price_history",
        )

    result = {"last_batch": None, "errors_24h": 0, "data_date": None}
    if last_batch:
        lb = dict(last_batch)
        _BTYPES = {"evening_jp": "夜(JP)", "morning_all": "朝(全)", "monthly": "月次"}
        result["last_batch"] = {
            "type": _BTYPES.get(lb.get("batch_type", ""), lb.get("batch_type", "")),
            "status": lb.get("status", ""),
            "time": lb.get("end_time", ""),
        }
    if error_count:
        result["errors_24h"] = dict(error_count).get("cnt", 0)
    if latest_price:
        result["data_date"] = dict(latest_price).get("d")
    return jsonify(result)


@app.route("/api/earnings-calendar")
def api_earnings_calendar():
    """保有銘柄の今後30日間の決算予定"""
    today_str = date.today().isoformat()
    with get_connection() as conn:
        rows = fetch_all(
            conn,
            """SELECT es.ticker, es.announcement_date,
                      COALESCE(h.name, u.name, es.ticker) AS name
               FROM earnings_schedule es
               INNER JOIN holdings h ON h.ticker = es.ticker AND h.is_closed = 0
               LEFT JOIN universe_master u ON u.ticker = es.ticker
               WHERE es.announcement_date >= ?
                 AND es.announcement_date <= date(?, '+30 days')
               ORDER BY es.announcement_date ASC""",
            (today_str, today_str),
        )
    events = []
    for r in rows:
        r = dict(r)
        ann_date = r.get("announcement_date", "")
        try:
            days_until = (datetime.strptime(ann_date, "%Y-%m-%d").date() - date.today()).days
        except ValueError:
            days_until = 99
        events.append({
            "ticker": r.get("ticker", ""),
            "name": r.get("name", ""),
            "date": ann_date,
            "days_until": days_until,
        })
    return jsonify({"events": events, "today": today_str})


@app.route("/api/screening-funnel")
def api_screening_funnel():
    """スクリーニングファネル統計（直近バッチ分）"""
    target = request.args.get("date")

    with get_connection() as conn:
        # 特定日付が指定された場合はその日付のみ、
        # 未指定の場合は JP/US それぞれの最新日付を使う
        if target:
            date_filter = "date = ?"
            date_params: tuple = (target,)
            signal_date_filter = "signal_date = ?"
            signal_date_params: tuple = (target,)
        else:
            market_dates = fetch_all(
                conn,
                "SELECT market, MAX(date) AS d FROM daily_signals GROUP BY market",
            )
            date_map = {dict(r)["market"]: dict(r)["d"] for r in market_dates}
            if not date_map:
                return jsonify({"error": "no data"}), 404
            target_dates = sorted(set(date_map.values()))
            target = " / ".join(
                f"{m}: {d}" for m, d in sorted(date_map.items())
            ) if len(target_dates) > 1 else target_dates[0]
            date_filter = "(market, date) IN (SELECT market, MAX(date) FROM daily_signals GROUP BY market)"
            date_params = ()
            signal_date_filter = "signal_date IN ({})".format(
                ",".join("?" for _ in target_dates)
            )
            signal_date_params = tuple(target_dates)

        universe = fetch_one(
            conn,
            "SELECT COUNT(*) AS cnt FROM universe_master "
            "WHERE is_active = 1 AND is_excluded = 0",
        )
        universe_cnt = dict(universe)["cnt"] if universe else 0

        screened = fetch_one(
            conn,
            f"SELECT COUNT(*) AS cnt FROM daily_signals WHERE {date_filter}",
            date_params,
        )
        screened_cnt = dict(screened)["cnt"] if screened else 0

        label_counts = fetch_all(
            conn,
            f"SELECT label, COUNT(*) AS cnt FROM daily_signals "
            f"WHERE {date_filter} GROUP BY label",
            date_params,
        )
        labels = {dict(r)["label"]: dict(r)["cnt"] for r in label_counts}

        notified = fetch_one(
            conn,
            f"SELECT COUNT(*) AS cnt FROM trade_signal_log WHERE {signal_date_filter}",
            signal_date_params,
        )
        notified_cnt = dict(notified)["cnt"] if notified else 0

        by_market_rows = fetch_all(
            conn,
            f"SELECT market, label, COUNT(*) AS cnt FROM daily_signals "
            f"WHERE {date_filter} GROUP BY market, label",
            date_params,
        )
        by_market: dict = {}
        for r in by_market_rows:
            r = dict(r)
            mk = r["market"]
            if mk not in by_market:
                by_market[mk] = {"screened": 0, "candidate": 0, "watch": 0, "skip": 0}
            by_market[mk]["screened"] += r["cnt"]
            by_market[mk][r["label"]] += r["cnt"]

        cond_a = fetch_one(
            conn,
            f"""SELECT COUNT(*) AS total,
                      SUM(cond_a_ma200_rising)  AS c1, SUM(cond_a_ma_aligned)    AS c2,
                      SUM(cond_a_52w_high_near) AS c3, SUM(cond_a_volume_surge)  AS c4,
                      SUM(cond_a_rsi_ok)        AS c5, SUM(cond_a_above_ma25)    AS c6
               FROM daily_signals
               WHERE {date_filter} AND cond_a_ma200_rising IS NOT NULL""",
            date_params,
        )
        cond_a = dict(cond_a) if cond_a else {}

        cond_b = fetch_one(
            conn,
            f"""SELECT COUNT(*) AS total,
                      SUM(cond_b_ma200_rising)     AS c1, SUM(cond_b_ma_aligned)       AS c2,
                      SUM(cond_b_pullback_touched) AS c3, SUM(cond_b_bounced)          AS c4,
                      SUM(cond_b_ma25_rising)      AS c5, SUM(cond_b_rsi_ok)           AS c6
               FROM daily_signals
               WHERE {date_filter} AND cond_b_ma200_rising IS NOT NULL""",
            date_params,
        )
        cond_b = dict(cond_b) if cond_b else {}

        score_dist = fetch_all(
            conn,
            f"""SELECT
                 CASE WHEN final_score >= 80 THEN '80-100'
                      WHEN final_score >= 60 THEN '60-79'
                      WHEN final_score >= 40 THEN '40-59'
                      WHEN final_score >= 20 THEN '20-39'
                      ELSE '0-19' END AS bucket,
                 COUNT(*) AS cnt
               FROM daily_signals WHERE {date_filter} AND final_score IS NOT NULL
               GROUP BY bucket ORDER BY bucket DESC""",
            date_params,
        )
        score_buckets = {dict(r)["bucket"]: dict(r)["cnt"] for r in score_dist}

    a_total = cond_a.get("total") or 1
    b_total = cond_b.get("total") or 1
    return jsonify({
        "date": target,
        "funnel": {
            "universe": universe_cnt,
            "screened": screened_cnt,
            "candidate": labels.get("candidate", 0),
            "watch": labels.get("watch", 0),
            "skip": labels.get("skip", 0),
            "notified": notified_cnt,
        },
        "by_market": by_market,
        "pattern_a": {
            "total": a_total,
            "ma200_rising": cond_a.get("c1") or 0,
            "ma_aligned": cond_a.get("c2") or 0,
            "high_52w_near": cond_a.get("c3") or 0,
            "volume_surge": cond_a.get("c4") or 0,
            "rsi_ok": cond_a.get("c5") or 0,
            "above_ma25": cond_a.get("c6") or 0,
        },
        "pattern_b": {
            "total": b_total,
            "ma200_rising": cond_b.get("c1") or 0,
            "ma_aligned": cond_b.get("c2") or 0,
            "pullback_touched": cond_b.get("c3") or 0,
            "bounced": cond_b.get("c4") or 0,
            "ma25_rising": cond_b.get("c5") or 0,
            "rsi_ok": cond_b.get("c6") or 0,
        },
        "score_distribution": score_buckets,
    })


# ============================================================
# リスク指標取得 / 計算 (Phase 3)
# ============================================================

def _get_or_calc_risk(conn, ticker: str, today_str: str, market: str) -> dict:
    """キャッシュがあればそこから、なければ計算して返す。"""
    cached = fetch_one(
        conn,
        "SELECT * FROM risk_snapshot WHERE ticker = ? AND calc_date = ?",
        (ticker, today_str),
    )
    if cached:
        return dict(cached)

    # 計算
    try:
        from src.risk import calc_risk_metrics
    except ImportError:
        return {}

    prices = fetch_all(
        conn,
        """SELECT adj_close FROM price_history
           WHERE ticker = ? AND is_holiday = 0 AND adj_close IS NOT NULL
           ORDER BY date DESC LIMIT 65""",
        (ticker,),
    )
    prices = [float(dict(p)["adj_close"]) for p in reversed(prices)]

    bm_code = "N225" if market == "JP" else "SP500"
    bm_prices = fetch_all(
        conn,
        """SELECT close FROM index_snapshot
           WHERE index_code = ?
           ORDER BY date DESC LIMIT 65""",
        (bm_code,),
    )
    bm_prices = [float(dict(p)["close"]) for p in reversed(bm_prices)]

    result = calc_risk_metrics(prices, bm_prices, market)

    # キャッシュに保存
    try:
        conn.execute(
            """INSERT OR REPLACE INTO risk_snapshot
               (ticker, calc_date, var_99_1d, beta_60d, sharpe_60d, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'))""",
            (ticker, today_str, result.get("var_99_1d"),
             result.get("beta_60d"), result.get("sharpe_60d")),
        )
        conn.commit()
    except Exception:
        pass

    return result


# ============================================================
# HTML レンダリング: /report
# ============================================================

def _render_report(today_str, market_env, holdings, candidates, market_history, llm_feedback=None, last_batch=None, data_insufficient=0, snapshot=None):
    market_env = dict(market_env) if market_env else {}
    holdings = [dict(h) for h in holdings]
    candidates = [dict(c) for c in candidates]
    market_history = [dict(m) for m in market_history]
    llm_feedback = [dict(f) for f in llm_feedback] if llm_feedback else []

    mode = market_env.get("mode", "unknown")
    mode_label = _MODE_LABEL.get(mode, mode)
    mode_css = _MODE_CSS.get(mode, "mode-normal")

    # --- サマリー計算（円換算） ---
    usdjpy = _get_usdjpy()
    total_pnl = 0.0
    total_cost = 0.0
    total_market_val = 0.0
    jp_count = 0
    us_count = 0
    no_price_tickers = []
    for h in holdings:
        ep = float(h["entry_price"] or 0)
        cp = float(h["current_price"] or 0) if h["current_price"] else 0
        qty = float(h["quantity"] or 1)
        mkt = h.get("market", "JP")
        fx = usdjpy if mkt != "JP" else 1.0
        if mkt == "JP":
            jp_count += 1
        else:
            us_count += 1
        if cp <= 0:
            no_price_tickers.append(h["ticker"])
            continue
        total_pnl += (cp - ep) * qty * fx
        total_cost += ep * qty * fx
        total_market_val += cp * qty * fx

    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0
    pnl_color_cls = "trend-up" if total_pnl >= 0 else "trend-down"

    # --- 総資産（スナップショット or フォールバック） ---
    if snapshot:
        total_assets = float(snapshot["total_assets_combined_jpy"])
        total_assets_source = "snapshot"
        snapshot_cash_jpy = float(snapshot.get("cash_jpy") or 0)
        snapshot_cash_usd = float(snapshot.get("cash_usd") or 0)
    else:
        # フォールバック: 保有銘柄の時価合計（on-the-fly 計算済み）
        total_assets = total_market_val
        total_assets_source = "fallback"
        snapshot_cash_jpy = 0.0
        snapshot_cash_usd = 0.0
    total_cash_jpy_equiv = snapshot_cash_jpy + snapshot_cash_usd * usdjpy

    no_price_warn = (
        f'<div class="freshness-warn" style="margin-top:4px">'
        f'⚠ 株価未取得のため除外: {", ".join(no_price_tickers)}</div>'
        if no_price_tickers else ""
    )

    # 市場環境乖離率
    n225 = market_env.get("n225_close")
    n225m = market_env.get("n225_ma25")
    sp500 = market_env.get("sp500_close")
    sp500m = market_env.get("sp500_ma25")
    n225_dev = f"{(n225 - n225m) / n225m * 100:+.1f}%" if n225 and n225m and n225m > 0 else "—"
    sp500_dev = f"{(sp500 - sp500m) / sp500m * 100:+.1f}%" if sp500 and sp500m and sp500m > 0 else "—"
    jp_str = f"日経平均: {n225:,.0f}（MA25乖離: {n225_dev}）" if n225 else "データなし"
    us_str = f"S&amp;P500: {sp500:,.0f}（MA25乖離: {sp500_dev}）" if sp500 else "データなし"
    env_date = market_env.get("date", "")

    # シグナル集計
    sig_a = sum(1 for c in candidates if c.get("pattern") == "A")
    sig_b = sum(1 for c in candidates if c.get("pattern") == "B")

    # バッチ鮮度情報（B-3）
    _BATCH_TYPE_LABEL = {"evening_jp": "夜バッチ(JP)", "morning_all": "朝バッチ(全体)", "monthly": "月次バッチ"}
    if last_batch:
        lb = dict(last_batch)
        batch_label = _BATCH_TYPE_LABEL.get(lb.get("batch_type", ""), lb.get("batch_type", ""))
        batch_time = lb.get("end_time", "不明")
        batch_status = lb.get("status", "")
        batch_status_mark = "✅" if batch_status == "success" else "⚠️"
        freshness_html = f'<div class="freshness">最終バッチ: {batch_label} {batch_status_mark} {escape(str(batch_time))}</div>'
    else:
        freshness_html = '<div class="freshness-warn">⚠ バッチ実行履歴なし</div>'

    # --- 今日の行動セクション構築 ---
    action_items = []
    for _ha in holdings:
        h = dict(_ha)
        cp = float(h["current_price"] or 0) if h["current_price"] else 0
        sl = float(h["stop_loss_price"] or 0)
        ep = float(h["entry_price"] or 0)
        if cp > 0 and sl > 0 and cp <= sl:
            action_items.append(("danger", f"🚨 {h['ticker']} {h.get('name', '')} — 損切りライン到達。手仕舞いを検討してください"))
        elif cp > 0 and sl > 0:
            sl_dist = (cp - sl) / cp * 100
            if sl_dist <= 2:
                action_items.append(("warn", f"⚠️ {h['ticker']} {h.get('name', '')} — 損切りラインまで残り{sl_dist:.1f}%。注視してください"))
        if cp > 0 and ep > 0:
            pnl = (cp - ep) / ep * 100
            if pnl <= -5:
                action_items.append(("warn", f"📉 {h['ticker']} {h.get('name', '')} — 含み損{pnl:.1f}%（早期警戒水準）"))
        # 利確ターゲット到達チェック
        _pt = h.get("profit_type")
        if _pt in ("I", "II") and cp > 0:
            from src.take_profit import _split_quantity
            _r_val = float(h.get("r_value") or 0)
            _qty_total = int(float(h.get("quantity") or 1)) or 1
            _t1_pct = float(h.get("t1_pct") or 50)
            _t2_pct = float(h.get("t2_pct") or 30)
            _t3_pct = float(h.get("t3_pct") or 20)
            _sq1, _sq2, _sq3 = _split_quantity(_qty_total, _t1_pct, _t2_pct, _t3_pct)
            _sell_map = {"t1": _sq1, "t2": _sq2, "t3": _sq3}
            for stage, label_str in [("t1", "T1"), ("t2", "T2"), ("t3", "T3")]:
                t_price = h.get(f"{stage}_price")
                t_reached = h.get(f"{stage}_reached")
                if t_price and not t_reached and cp >= t_price:
                    _r_mult = f"+{(t_price - ep) / _r_val:.1f}R" if _r_val > 0 else ""
                    _sell_n = _sell_map.get(stage, 0)
                    _sell_str = f"{_sell_n}株売却" if _sell_n > 0 else "売却検討"
                    action_items.append((
                        "info",
                        f"🎯 {h['ticker']} {h.get('name', '')} — "
                        f"{label_str}到達（{_r_mult}）。{_sell_str}を検討"
                    ))
            # SL引き上げ推奨
            _adj = h.get("adjusted_stop")
            if _adj and _adj > sl:
                action_items.append((
                    "info",
                    f"📋 {h['ticker']} — 損切りライン引き上げ推奨: "
                    f"{sl:,.2f} → {_adj:,.2f}（買値付近）",
                    {"type": "sl_update", "ticker": h["ticker"],
                     "old_sl": sl, "new_sl": round(float(_adj), 2)},
                ))

    if mode in ("caution", "full_caution"):
        action_items.append(("info", f"🔴 {mode_label}中 — 新規検討候補は自動停止しています"))
    elif len(candidates) > 0:
        today_sigs = [c for c in candidates if c.get("signal_date") == today_str]
        if today_sigs:
            action_items.append(("info", f"📈 本日の検討候補: {len(today_sigs)}件（最終判断はご自身で行ってください）"))
        else:
            action_items.append(("ok", "✅ 本日新たな検討候補はありません"))
    else:
        action_items.append(("ok", "✅ 本日新たな検討候補はありません"))

    # データ不足の銘柄数（B-2: 見送り理由）
    if data_insufficient > 0:
        action_items.append(("info", f"⚠️ データ不足のため一部条件を判定保留: {data_insufficient}銘柄"))

    # 保有枠の状況
    remaining_slots = _SKIP_MAX_HOLDINGS - len(holdings)
    if remaining_slots <= 0:
        action_items.append(("warn", f"🈵 保有枠が満杯です（{len(holdings)}/{_SKIP_MAX_HOLDINGS}）— 新規エントリーは既存ポジション整理後に検討してください"))
    else:
        action_items.append(("info", f"📦 保有枠: {len(holdings)}/{_SKIP_MAX_HOLDINGS}（残り{remaining_slots}枠）"))

    if not action_items:
        action_items.append(("ok", "✅ 特に対応が必要な事項はありません"))

    # --- LLM アドバイザーカードリスト構築 ---
    _LLM_DISPLAY = {
        "openai": ("ChatGPT", "#10a37f", "💬"),
        "anthropic": ("Claude", "#d97706", "🧠"),
        "gemini": ("Gemini", "#4285F4", "✨"),
    }
    llm_cards = []
    for fb in llm_feedback:
        prov = fb.get("provider", "")
        display_name, color, icon = _LLM_DISPLAY.get(prov, (prov, "#999", "💬"))
        batch_label_llm = "夜バッチ" if fb.get("batch_type") == "evening" else "朝バッチ"
        llm_cards.append({
            "feedback_date": fb.get("feedback_date", ""),
            "color": color,
            "icon": icon,
            "display_name": display_name,
            "model": fb.get("model", ""),
            "response_text": fb.get("response_text", ""),
            "batch_label": batch_label_llm,
            "created_at": fb.get("created_at", ""),
        })

    # セクター配分用データ（holdings から・円換算）
    sector_map = {}
    for h in holdings:
        sec = h.get("sector") or "不明"
        val = float(h["entry_price"] or 0) * float(h["quantity"] or 1)
        fx = usdjpy if h.get("market", "JP") != "JP" else 1.0
        sector_map[sec] = sector_map.get(sec, 0) + val * fx
    sector_labels = json.dumps(list(sector_map.keys()), ensure_ascii=False)
    sector_values = json.dumps([round(v) for v in sector_map.values()])

    # --- 保有銘柄テーブル行リスト構築 ---
    holding_rows = []
    for _h in holdings:
        h = dict(_h)  # sqlite3.Row → dict（.get() を使えるようにする）
        ep = float(h["entry_price"] or 0)
        cp = float(h["current_price"] or 0) if h["current_price"] else None
        sl = float(h["stop_loss_price"] or 0)
        tp = float(h["target_price"] or 0) if h["target_price"] else None
        qty = h["quantity"]

        pnl_pct = 0.0
        pnl_str = "—"
        pnl_jpy_str = ""
        pnl_color = ""
        market = h.get("market") or "JP"
        fx = usdjpy if market != "JP" else 1.0
        if cp and ep:
            pnl_pct = (cp - ep) / ep * 100
            pnl_color = "text-profit" if pnl_pct >= 0 else "text-loss"
            pnl_str = f"{pnl_pct:+.1f}%"
            pnl_jpy = (cp - ep) * float(qty or 1) * fx
            pnl_jpy_str = f"¥{pnl_jpy:+,.0f}"

        cp_str = f"{cp:,.2f}" if cp else "—"

        # 利確ターゲット表示の構築
        profit_type = h.get("profit_type")
        # profit_type 未設定の場合、signal_pattern から推定
        if not profit_type:
            sp = h.get("signal_pattern")
            if sp == "A":
                profit_type = "I"
            elif sp == "B":
                profit_type = "II"
            else:
                profit_type = "III"
        target_str = "—"
        target_html = "—"
        if profit_type in ("I", "II") and cp:
            target_html = _build_tp_target_html(h, cp)
        elif profit_type == "III":
            target_html = "<span class='tp-type-label'>手動管理</span>"
            if tp and cp:
                dist = (tp - cp) / cp * 100
                target_html += f"<br><span class='tp-pending'>目安 {tp:,.2f}（{dist:+.1f}%）</span>"
        elif tp and cp:
            dist = (tp - cp) / cp * 100
            target_html = f"{tp:,.2f}（{dist:+.1f}%）"
        target_str = target_html

        days_str = "—"
        days_css = ""
        if h["entry_date"]:
            try:
                entry_dt = datetime.strptime(h["entry_date"], "%Y-%m-%d").date()
                holding_days = (date.today() - entry_dt).days
                days_str = f"{holding_days}日"
                if holding_days > 60:
                    days_css = "text-loss"
                elif holding_days > 30:
                    days_css = "trend-flat"
            except ValueError:
                pass

        # テクニカル指標
        rsi = h.get("rsi14")
        rsi_str = f"{rsi:.0f}" if rsi is not None else "—"
        rsi_css = ""
        if rsi is not None:
            if rsi >= 70:
                rsi_css = "rsi-high"
            elif rsi <= 30:
                rsi_css = "rsi-low"

        atr = h.get("atr14")
        atr_str = f"{atr:.1f}" if atr is not None else "—"

        # MA25乖離率
        ma25 = h.get("ma25")
        ma25_dev = 0.0
        if cp and ma25 and ma25 > 0:
            ma25_dev = (cp - ma25) / ma25 * 100
            ma25_dev_str = f"{ma25_dev:+.1f}%"
        else:
            ma25_dev_str = "—"

        # スコア & ランク
        fscore = h.get("final_score")
        frank = h.get("recommend_rank")
        score_str = f"{fscore:.0f}" if fscore is not None else "—"
        rank_css = _RANK_CSS.get(frank, "rank-default")
        rank_html = f"<span class='rank-badge {rank_css}'>{escape(frank or '—')}</span>"

        # トレンド矢印
        ma5 = h.get("ma5")
        ma75 = h.get("ma75")
        ma200 = h.get("ma200")
        if ma5 and ma25 and ma75 and ma200:
            if ma5 >= ma25 >= ma75 >= ma200:
                trend_str = "<span class='trend-up'>&#9650; 上昇</span>"
            elif ma25 >= ma75 >= ma200:
                trend_str = "<span class='trend-up'>&#9651; 中期上昇</span>"
            elif ma75 >= ma200:
                trend_str = "<span class='trend-flat'>&#9644; 横ばい</span>"
            else:
                trend_str = "<span class='trend-down'>&#9660; 下降</span>"
        else:
            trend_str = "—"

        # 状態バッジ（信号機方式: 損益 + トレンド + SL距離 + 利確到達で総合判定）
        sl_dist_pct = ((cp - sl) / cp * 100) if cp and sl else 99
        # 利確到達チェック: いずれかのTに到達しているか
        _any_tp_reached = (
            profit_type in ("I", "II")
            and cp
            and (
                (h.get("t1_price") and not h.get("t1_reached") and cp >= h["t1_price"])
                or (h.get("t2_price") and not h.get("t2_reached") and cp >= h["t2_price"])
                or (h.get("t3_price") and not h.get("t3_reached") and cp >= h["t3_price"])
            )
        )
        if pnl_pct <= -5 or sl_dist_pct <= 2:
            status_cls = "status-danger"
            status_label = "危険"
        elif _any_tp_reached:
            status_cls = "status-tp"
            status_label = "利確検討"
        elif pnl_pct < 0 or (rsi is not None and rsi >= 70) or sl_dist_pct <= 5:
            status_cls = "status-warn"
            status_label = "注意"
        else:
            status_cls = "status-safe"
            status_label = "安心"

        holding_rows.append({
            "ticker": h["ticker"],
            "name": h.get("name") or "",
            "cp_str": cp_str,
            "pnl_css": pnl_color,
            "pnl_str": pnl_str,
            "pnl_jpy_str": pnl_jpy_str,
            "status_cls": status_cls,
            "status_label": status_label,
            "sl_str": f"{sl:,.2f}",
            "sl_raw": sl,
            "adjusted_stop": float(h["adjusted_stop"]) if h.get("adjusted_stop") else None,
            "target_str": target_str,
            "days_str": days_str,
            "days_css": days_css,
            "market": h.get("market") or "",
            "qty_str": f"{qty:.0f}" if qty else "—",
            "rsi_css": rsi_css,
            "rsi_str": rsi_str,
            "atr_str": atr_str,
            "ma25_dev_str": ma25_dev_str,
            "score_str": score_str,
            "rank_html": rank_html,
            "trend_str": trend_str,
        })

    # シグナル履歴テーブル行リスト構築
    signal_rows = []
    for s in candidates:
        sl_p = float(s["stop_loss_pct"] or 0)
        tg_p = sl_p * 2
        amt = s["recommended_amount"]
        ep = float(s["entry_price"])
        currency = "円" if s["market"] == "JP" else "USD"
        amt_str = f"{amt:,.0f} {currency}" if amt else "—"
        # 株数換算（B-4）
        if amt and ep > 0:
            est_qty = amt / ep
            qty_str = f"約{est_qty:.0f}株" if est_qty >= 1 else f"約{est_qty:.2f}株"
        else:
            qty_str = "—"
        signal_rows.append({
            "signal_date": s.get("signal_date") or "",
            "ticker": s["ticker"],
            "name": s.get("name") or "",
            "market": s.get("market") or "",
            "pattern_name": _PATTERN_NAME.get(s["pattern"], s["pattern"]),
            "entry_str": f"{ep:,.2f}",
            "sl_str": f"{float(s['stop_loss_price']):,.2f}（-{sl_p:.1f}%）",
            "target_str": f"{float(s['target_price']):,.2f}（+{tg_p:.1f}%）",
            "amt_str": amt_str,
            "qty_str": qty_str,
        })

    # 市場環境タイムラインの JSON データ
    mh_dates = json.dumps([m["date"] for m in market_history])
    mh_n225 = json.dumps([m["n225_close"] for m in market_history])
    mh_n225_ma = json.dumps([m["n225_ma25"] for m in market_history])
    mh_sp500 = json.dumps([m["sp500_close"] for m in market_history])
    mh_sp500_ma = json.dumps([m["sp500_ma25"] for m in market_history])
    mh_modes = json.dumps([m["mode"] for m in market_history])

    return render_template(
        "report.html",
        active_page="report",
        today_str=today_str,
        total_market_val=total_market_val,
        total_cost=total_cost,
        total_pnl=total_pnl,
        total_pnl_pct=total_pnl_pct,
        pnl_color_cls=pnl_color_cls,
        no_price_warn=no_price_warn,
        mode_label=mode_label,
        mode_css=mode_css,
        env_date=env_date,
        jp_str=jp_str,
        us_str=us_str,
        freshness_html=freshness_html,
        jp_count=jp_count,
        us_count=us_count,
        sig_a=sig_a,
        sig_b=sig_b,
        action_items=action_items,
        holdings=holdings,
        candidates=candidates,
        llm_cards=llm_cards,
        holding_rows=holding_rows,
        signal_rows=signal_rows,
        sector_labels=sector_labels,
        sector_values=sector_values,
        mh_dates=mh_dates,
        mh_n225=mh_n225,
        mh_n225_ma=mh_n225_ma,
        mh_sp500=mh_sp500,
        mh_sp500_ma=mh_sp500_ma,
        mh_modes=mh_modes,
        skip_sl_min_pct=_SKIP_SL_MIN_PCT,
        skip_atr_mult=_SKIP_ATR_MULT,
        skip_capital_jpy=_SKIP_CAPITAL_JPY,
        skip_capital_usd=_SKIP_CAPITAL_USD,
        skip_risk_pct=_SKIP_RISK_PCT,
        skip_max_holdings=_SKIP_MAX_HOLDINGS,
        usdjpy_rate=usdjpy,
        total_assets=total_assets,
        total_assets_source=total_assets_source,
        total_cash_jpy_equiv=total_cash_jpy_equiv,
    )


# ============================================================
# HTML レンダリング: /stock/<ticker>
# ============================================================

def _render_stock_detail(ticker, master, tech, price, prev_price, range_52w,
                         score, signals, fundamentals, risk_data):
    name = master.get("name", "")
    market = master.get("market", "")
    sector = master.get("sector") or "—"
    industry = master.get("industry") or "—"

    cp = float(price.get("adj_close") or price.get("close") or 0)
    pp = float(dict(prev_price).get("adj_close") or 0) if prev_price else 0
    change_pct = ((cp - pp) / pp * 100) if pp > 0 else 0
    change_color = "trend-up" if change_pct >= 0 else "trend-down"

    h52 = range_52w.get("high_52w")
    l52 = range_52w.get("low_52w")
    h52_str = f"{h52:,.2f}" if h52 else "—"
    l52_str = f"{l52:,.2f}" if l52 else "—"

    # テクニカル指標
    def _tv(key):
        v = tech.get(key)
        return f"{v:,.2f}" if v is not None else "—"

    rsi = tech.get("rsi14")
    rsi_str = f"{rsi:.1f}" if rsi is not None else "—"
    rsi_bar_color = "var(--color-danger)" if rsi and rsi >= 70 else ("var(--color-success)" if rsi and rsi <= 30 else "var(--color-primary)")
    rsi_width = rsi if rsi is not None else 0

    vol = tech.get("vol_20d_avg")
    vol_str = f"{vol:,.0f}" if vol else "—"

    # スコア
    fscore = score.get("final_score")
    frank = score.get("recommend_rank") or "—"
    reason = score.get("recommend_reason") or "—"
    rank_css = _RANK_CSS.get(frank, "rank-default")

    # ファンダメンタルズ
    has_fundamentals = bool(fundamentals and fundamentals.get("per"))
    fund_rows = []
    if has_fundamentals:
        def _fv(key, fmt=".2f"):
            v = fundamentals.get(key)
            return f"{v:{fmt}}" if v is not None else "—"
        fund_rows = [
            ("PER", _fv("per")),
            ("PBR", _fv("pbr")),
            ("配当利回り", f"{_fv('dividend_yield', '.2f')}%"),
            ("EPS", _fv("eps")),
            ("BPS", _fv("bps")),
        ]

    # リスク指標
    var_val = risk_data.get("var_99_1d")
    beta_val = risk_data.get("beta_60d")
    sharpe_val = risk_data.get("sharpe_60d")
    var_str = f"{var_val * 100:.2f}%" if var_val is not None else "計算不可"
    beta_str = f"{beta_val:.2f}" if beta_val is not None else "計算不可"
    sharpe_str = f"{sharpe_val:.2f}" if sharpe_val is not None else "計算不可"

    # テクニカル指標行リスト
    tech_rows = [
        ("atr14", "ATR (14)"),
        ("ma5", "MA5"),
        ("ma25", "MA25"),
        ("ma75", "MA75"),
        ("ma200", "MA200"),
    ]
    tech_values = {key: _tv(key) for key, _ in tech_rows}

    # シグナル履歴リスト
    sig_rows = []
    for s in signals:
        s = dict(s)
        sl_p = float(s.get("stop_loss_pct") or 0)
        sig_rows.append({
            "signal_date": s.get("signal_date") or "",
            "pattern_name": _PATTERN_NAME.get(s.get("pattern", ""), s.get("pattern", "")),
            "entry_str": f"{float(s.get('entry_price') or 0):,.2f}",
            "sl_str": f"{float(s.get('stop_loss_price') or 0):,.2f}（-{sl_p:.1f}%）",
            "target_str": f"{float(s.get('target_price') or 0):,.2f}",
            "market_mode": s.get("market_mode") or "",
        })

    return render_template(
        "stock_detail.html",
        active_page="",
        ticker=ticker,
        name=name,
        market=market,
        sector=sector,
        industry=industry,
        cp=cp,
        change_pct=change_pct,
        change_color=change_color,
        h52_str=h52_str,
        l52_str=l52_str,
        fscore=fscore,
        frank=frank,
        rank_css=rank_css,
        reason=reason,
        rsi_str=rsi_str,
        rsi_width=rsi_width,
        rsi_bar_color=rsi_bar_color,
        tech_rows=tech_rows,
        tech_values=tech_values,
        vol_str=vol_str,
        has_fundamentals=has_fundamentals,
        fund_rows=fund_rows,
        var_str=var_str,
        beta_str=beta_str,
        sharpe_str=sharpe_str,
        sig_rows=sig_rows,
    )


# ============================================================
# HTML レンダリング: /performance
# ============================================================

def _render_performance(summary, pattern_stats, all_trades, usdjpy_rate: float = 150.0):
    summary = summary or {}
    pattern_stats = [dict(p) for p in pattern_stats]
    all_trades = [dict(t) for t in all_trades]

    total = summary.get("total") or 0
    wins = summary.get("wins") or 0
    losses = summary.get("losses") or 0
    avg_pnl = summary.get("avg_pnl") or 0
    best = summary.get("best_trade") or 0
    worst = summary.get("worst_trade") or 0
    total_profit = summary.get("total_profit") or 0
    total_loss = summary.get("total_loss") or 0
    win_rate = (wins / total * 100) if total > 0 else 0
    pf = (total_profit / total_loss) if total_loss > 0 else 0

    # 実現損益（円）: JP はそのまま、US は USD→JPY 換算
    total_pnl_jpy_raw = float(summary.get("total_pnl_jpy") or 0)
    total_pnl_usd_raw = float(summary.get("total_pnl_usd") or 0)
    total_pnl_combined_jpy = total_pnl_jpy_raw + total_pnl_usd_raw * usdjpy_rate

    # パターン別リスト
    pat_rows = []
    for p in pattern_stats:
        pat_name = _PATTERN_NAME.get(p.get("pattern", ""), p.get("pattern", ""))
        pt = p.get("total") or 0
        pw = p.get("wins") or 0
        pa = p.get("avg_pnl") or 0
        pwr = (pw / pt * 100) if pt > 0 else 0
        p_jpy = float(p.get("sum_pnl_jpy") or 0)
        p_usd = float(p.get("sum_pnl_usd") or 0)
        pat_rows.append({
            "pattern_name": pat_name,
            "total": pt,
            "wins": pw,
            "win_rate": pwr,
            "avg_pnl": pa,
            "sum_pnl_jpy": p_jpy + p_usd * usdjpy_rate,
        })

    # トレード一覧リスト
    trade_rows = []
    for t in reversed(all_trades[-50:]):  # 直近50件
        pnl = float(t.get("pnl_pct") or 0)
        reason_label = {
            "stop_loss": "損切り", "take_profit": "利確",
            "manual": "手動", "forced_stop": "強制停止",
        }.get(t.get("exit_reason", ""), t.get("exit_reason") or "—")
        pat_name = _PATTERN_NAME.get(t.get("pattern", ""), t.get("pattern", ""))
        # 損益金額（円換算）
        raw_amount = t.get("pnl_amount")
        market = t.get("market") or "JP"
        if raw_amount is not None:
            pnl_amount_jpy = float(raw_amount) * (usdjpy_rate if market == "US" else 1.0)
        else:
            pnl_amount_jpy = None
        trade_rows.append({
            "exit_date": t.get("actual_exit_date") or "",
            "ticker": t.get("ticker") or "",
            "pattern_name": pat_name,
            "pnl": pnl,
            "pnl_amount_jpy": pnl_amount_jpy,
            "reason_label": reason_label,
        })

    return render_template(
        "performance.html",
        active_page="performance",
        total=total,
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        avg_pnl=avg_pnl,
        pf=pf,
        best=best,
        worst=worst,
        total_pnl_jpy=total_pnl_combined_jpy,
        pat_rows=pat_rows,
        trade_rows=trade_rows,
    )


# ============================================================
# HTML レンダリング: /screening
# ============================================================

def _cond_dot(value: int | None, label: str) -> str:
    """条件フラグを色付きドットで描画する。"""
    css = "cond-dot-met" if value else "cond-dot-na"
    return f'<span class="tip cond-dot {css}" data-tip="{label}"></span>'


def _render_screening(target_date: str, rows: list[dict]) -> str:
    total = len(rows)
    cand_cnt = sum(1 for r in rows if r["label"] == "candidate")
    watch_cnt = sum(1 for r in rows if r["label"] == "watch")
    skip_cnt = sum(1 for r in rows if r["label"] == "skip")

    # --- テーブル行リスト（row_html を pre-render）---
    table_rows = []
    for r in rows:
        ticker = r["ticker"] or ""
        name = r.get("name") or ""
        market = r.get("market") or ""
        pattern = r.get("pattern") or ""
        label = r.get("label") or "skip"
        close = r.get("close")
        rsi = r.get("rsi14")
        vol_ratio = r.get("vol_ratio")
        fscore = r.get("final_score")
        frank = r.get("recommend_rank") or "—"
        sector = r.get("sector") or "—"

        # ラベルバッジ
        lbl_css = _LABEL_CSS.get(label, "label-skip")
        lbl_text = _LABEL_DISPLAY.get(label, label)

        # ランクバッジ
        rank_css = _RANK_CSS.get(frank, "rank-default")

        # 条件フラグドット
        conds_html = ""
        cond_labels = {"A": _COND_A_LABELS, "B": _COND_B_LABELS, "C": _COND_C_LABELS}.get(pattern, _COND_A_LABELS)
        for col, tip in cond_labels:
            conds_html += _cond_dot(r.get(col), tip)

        # パターン表示
        pat_name = _PATTERN_NAME.get(pattern, "—")
        pat_html = f'<span class="tip" data-tip="{escape(pat_name)}">{escape(pattern)}</span>' if pattern else "—"

        # Score 表示
        score_str = f"{fscore:.0f}" if fscore is not None else "—"

        # 買い判定 — 条件フラグから未達の条件名を列挙
        reason_text = r.get("recommend_reason") or ""
        met_count = sum(1 for col, _ in cond_labels if r.get(col) == 1)
        total_conds = len(cond_labels)
        failed_names = [tip for col, tip in cond_labels if r.get(col) == 0]
        if label == "candidate":
            verdict_html = '<span class="verdict-candidate">買い候補</span>'
        elif label == "watch":
            verdict_html = '<span class="verdict-watch">監視</span>'
        else:
            verdict_html = '<span class="verdict-skip">対象外</span>'
        # 未達条件を赤で表示
        if failed_names:
            failed_html = ", ".join(
                f'<span class="verdict-failed">{escape(n)}</span>' for n in failed_names
            )
            verdict_detail = f'{verdict_html} ({met_count}/{total_conds})<br><span class="reason-text">未達: {failed_html}</span>'
        else:
            verdict_detail = f'{verdict_html} ({met_count}/{total_conds})'
        # 理由
        reason_html = f'<span class="reason-text">{escape(reason_text)}</span>' if reason_text else "—"

        # 詳細列
        close_str = f"{close:,.2f}" if close else "—"
        rsi_str = f"{rsi:.0f}" if rsi is not None else "—"
        rsi_css = ""
        if rsi is not None:
            if rsi >= 70:
                rsi_css = "rsi-high"
            elif rsi <= 30:
                rsi_css = "rsi-low"
        vr_str = f"{vol_ratio:.1f}x" if vol_ratio is not None else "—"

        row_html = (
            f'<tr data-label="{escape(label)}" data-market="{escape(market)}"'
            f' data-pattern="{escape(pattern)}" data-score="{fscore or 0}">'
            f'<td><a class="ticker-link" href="/stock/{escape(ticker)}">{escape(ticker)}</a></td>'
            f'<td class="mobile-hide">{escape(name)}</td>'
            f'<td class="mobile-hide">{escape(market)}</td>'
            f"<td>{pat_html}</td>"
            f'<td><span class="rank-badge {lbl_css}">{lbl_text}</span></td>'
            f'<td class="mobile-hide" style="white-space:nowrap">{conds_html}</td>'
            f"<td>{score_str}</td>"
            f'<td><span class="rank-badge {rank_css}">{escape(frank)}</span></td>'
            f'<td class="mobile-hide">{verdict_detail}</td>'
            f'<td class="mobile-hide">{reason_html}</td>'
            f'<td class="detail-col">{close_str}</td>'
            f'<td class="detail-col {rsi_css}">{rsi_str}</td>'
            f'<td class="detail-col">{vr_str}</td>'
            f'<td class="detail-col">{escape(sector)}</td>'
            f"</tr>"
        )
        table_rows.append({"row_html": row_html})

    return render_template(
        "screening.html",
        active_page="screening",
        target_date=target_date,
        total=total,
        cand_cnt=cand_cnt,
        watch_cnt=watch_cnt,
        skip_cnt=skip_cnt,
        table_rows=table_rows,
    )


# ============================================================
# ルーティング: /settings（設定ページ）
# ============================================================

# ダッシュボードから編集可能な設定項目の定義
# (section, key, label, type, default, min, max, step, help)
_EDITABLE_SETTINGS: list[dict] = [
    # --- 資金管理 ---
    {"section": "capital", "key": "total_capital_jpy", "label": "総資金（円）",
     "type": "int", "default": 200000, "min": 0, "max": 100_000_000, "step": 10000,
     "help": "日本株の推奨投入額計算に使用", "group": "capital"},
    {"section": "capital", "key": "total_capital_usd", "label": "総資金（USD）",
     "type": "int", "default": 1500, "min": 0, "max": 1_000_000, "step": 100,
     "help": "米国株の推奨投入額計算に使用", "group": "capital"},
    {"section": "capital", "key": "max_risk_per_trade_pct", "label": "1トレードあたりリスク（%）",
     "type": "float", "default": 2.0, "min": 0.1, "max": 10.0, "step": 0.1,
     "help": "推奨投入額 = 総資金 × この値 ÷ 損切り幅", "group": "capital"},
    {"section": "capital", "key": "max_concurrent_holdings", "label": "最大同時保有数",
     "type": "int", "default": 3, "min": 1, "max": 50, "step": 1,
     "help": "これを超えると新規シグナルが見送りになる", "group": "capital"},
    {"section": "capital", "key": "default_stop_loss_pct", "label": "デフォルト損切り（%）",
     "type": "float", "default": 8.0, "min": 1.0, "max": 30.0, "step": 0.5,
     "help": "holdings.csv で stop_loss_price 省略時に使用", "group": "capital"},
    {"section": "capital", "key": "usdjpy_fallback", "label": "USD/JPY フォールバック",
     "type": "float", "default": 150.0, "min": 50.0, "max": 300.0, "step": 0.1,
     "help": "為替レート取得失敗時の代替値", "group": "capital"},
    {"section": "investment_goal", "key": "monthly_deposit_jpy", "label": "毎月の入金額（円）",
     "type": "int", "default": 10000, "min": 0, "max": 1_000_000, "step": 1000,
     "help": "月次レポートで入金控除後リターンを計算するために使用", "group": "investment_goal"},
    {"section": "investment_goal", "key": "target_monthly_return_pct", "label": "月次利益目標（%）",
     "type": "float", "default": 15.0, "min": 0.0, "max": 100.0, "step": 0.5,
     "help": "保証ではなく検証目標として扱う", "group": "investment_goal"},
    {"section": "risk_control", "key": "monthly_loss_stop_pct", "label": "月間損失停止ライン（%）",
     "type": "float", "default": 6.0, "min": 0.0, "max": 50.0, "step": 0.5,
     "help": "月間の確定損失がこの割合に達したら新規買いを停止", "group": "risk_control"},
    {"section": "risk_control", "key": "max_consecutive_losses_before_pause", "label": "連続損失停止回数",
     "type": "int", "default": 3, "min": 0, "max": 20, "step": 1,
     "help": "この回数だけ損失が続いたら新規買いを停止", "group": "risk_control"},
    # --- シグナル パターンA ---
    {"section": "signal", "key": "pattern_a_rsi_min", "label": "パターンA RSI下限",
     "type": "int", "default": 50, "min": 0, "max": 100, "step": 1,
     "help": "", "group": "signal_a"},
    {"section": "signal", "key": "pattern_a_rsi_max", "label": "パターンA RSI上限",
     "type": "int", "default": 70, "min": 0, "max": 100, "step": 1,
     "help": "", "group": "signal_a"},
    {"section": "signal", "key": "pattern_a_volume_ratio_min", "label": "パターンA 出来高倍率",
     "type": "float", "default": 1.2, "min": 0.5, "max": 10.0, "step": 0.1,
     "help": "20日平均出来高に対する倍率", "group": "signal_a"},
    {"section": "signal", "key": "pattern_a_52w_high_pct", "label": "パターンA 52週高値近傍（%）",
     "type": "float", "default": 0.92, "min": 0.5, "max": 1.0, "step": 0.01,
     "help": "終値 ≥ 52週高値 × この値 で条件成立", "group": "signal_a"},
    # --- シグナル パターンB ---
    {"section": "signal", "key": "pattern_b_rsi_min", "label": "パターンB RSI下限",
     "type": "int", "default": 40, "min": 0, "max": 100, "step": 1,
     "help": "", "group": "signal_b"},
    {"section": "signal", "key": "pattern_b_rsi_max", "label": "パターンB RSI上限",
     "type": "int", "default": 60, "min": 0, "max": 100, "step": 1,
     "help": "", "group": "signal_b"},
    {"section": "signal", "key": "pattern_b_ma25_touch_pct", "label": "パターンB MA25タッチ閾値",
     "type": "float", "default": 0.98, "min": 0.9, "max": 1.0, "step": 0.01,
     "help": "安値 ≤ MA25 × (1/この値) で押し目タッチ判定", "group": "signal_b"},
    {"section": "signal", "key": "pattern_b_lookback_days", "label": "パターンB ルックバック日数",
     "type": "int", "default": 5, "min": 1, "max": 20, "step": 1,
     "help": "直近N日以内にMA25タッチを検索", "group": "signal_b"},
    # --- シグナル 共通 ---
    {"section": "signal", "key": "atr_stop_multiplier", "label": "ATR損切り倍率",
     "type": "float", "default": 2.0, "min": 0.5, "max": 5.0, "step": 0.1,
     "help": "損切りライン = エントリー価格 − ATR14 × この値", "group": "signal_common"},
    {"section": "signal", "key": "risk_reward_ratio", "label": "リスクリワード比",
     "type": "float", "default": 2.0, "min": 1.0, "max": 10.0, "step": 0.1,
     "help": "利確目安 = 損切り幅 × この値", "group": "signal_common"},
    {"section": "signal", "key": "max_candidates_per_pattern", "label": "パターンあたり最大候補数",
     "type": "int", "default": 3, "min": 1, "max": 20, "step": 1,
     "help": "スコア上位N銘柄のみ通知", "group": "signal_common"},
    {"section": "signal", "key": "stop_loss_warning_min_pct", "label": "損切り幅 下限警告（%）",
     "type": "float", "default": 1.0, "min": 0.1, "max": 5.0, "step": 0.1,
     "help": "損切り幅がこの値未満の場合は見送り", "group": "signal_common"},
    {"section": "signal", "key": "stop_loss_warning_max_pct", "label": "損切り幅 上限警告（%）",
     "type": "float", "default": 15.0, "min": 5.0, "max": 30.0, "step": 0.5,
     "help": "損切り幅がこの値超の場合は見送り", "group": "signal_common"},
    # --- スクリーニング ---
    {"section": "screening", "key": "jp_min_price", "label": "JP 最低株価（円）",
     "type": "int", "default": 300, "min": 0, "max": 100000, "step": 100,
     "help": "", "group": "screening"},
    {"section": "screening", "key": "jp_min_turnover", "label": "JP 最低売買代金（円）",
     "type": "int", "default": 300_000_000, "min": 0, "max": 10_000_000_000, "step": 100_000_000,
     "help": "20日平均売買代金", "group": "screening"},
    {"section": "screening", "key": "jp_min_market_cap", "label": "JP 最低時価総額（円）",
     "type": "int", "default": 30_000_000_000, "min": 0, "max": 1_000_000_000_000, "step": 1_000_000_000,
     "help": "", "group": "screening"},
    {"section": "screening", "key": "us_min_price", "label": "US 最低株価（USD）",
     "type": "int", "default": 5, "min": 0, "max": 10000, "step": 1,
     "help": "", "group": "screening"},
    {"section": "screening", "key": "us_min_turnover", "label": "US 最低売買代金（USD）",
     "type": "int", "default": 20_000_000, "min": 0, "max": 1_000_000_000, "step": 1_000_000,
     "help": "20日平均売買代金", "group": "screening"},
    {"section": "screening", "key": "us_min_market_cap", "label": "US 最低時価総額（USD）",
     "type": "int", "default": 2_000_000_000, "min": 0, "max": 100_000_000_000, "step": 100_000_000,
     "help": "", "group": "screening"},
    {"section": "screening", "key": "min_active_days_of_30", "label": "最低取引日数（30日中）",
     "type": "int", "default": 20, "min": 1, "max": 30, "step": 1,
     "help": "直近30日でこの日数以上取引がある銘柄のみ対象", "group": "screening"},
    # --- ペーパーモード ---
    {"section": "screening", "key": "allow_unknown_market_cap", "label": "時価総額不明を許可",
     "type": "bool", "default": False, "min": None, "max": None, "step": None,
     "help": "無効の場合、時価総額が計算できない銘柄は除外", "group": "screening"},
    {"section": "paper_mode", "key": "enabled", "label": "ペーパーモード",
     "type": "bool", "default": False, "min": None, "max": None, "step": None,
     "help": "有効にするとシグナル発生時に仮想エントリーを記録し検証する", "group": "paper_mode"},
    {"section": "paper_mode", "key": "entry_slippage_pct", "label": "仮想約定スリッページ（%）",
     "type": "float", "default": 0.0, "min": 0.0, "max": 5.0, "step": 0.1,
     "help": "翌営業日始値で約定するときに上乗せする仮想コスト", "group": "paper_mode"},
]

_GROUP_LABELS = {
    "capital": "資金管理",
    "investment_goal": "投資目標",
    "risk_control": "運用停止ルール",
    "signal_a": "シグナル — パターンA（モメンタムブレイクアウト）",
    "signal_b": "シグナル — パターンB（押し目買い）",
    "signal_common": "シグナル — 共通",
    "screening": "スクリーニング",
    "paper_mode": "ペーパーモード",
}

# ============================================================
# PayPay ポイント運用ページ
# ============================================================

# ============================================================
# 投資ルール確認ページ
# ============================================================

@app.route("/rules")
def rules_page():
    """初心者向けに、システムが守る投資ルールを一覧表示する。"""
    context = _build_rules_context()
    return render_template("rules.html", active_page="rules", **context)


def _build_rules_context() -> dict:
    cap = CONFIG.get("capital", {})
    sig = CONFIG.get("signal", {})
    screening = CONFIG.get("screening", {})
    goal = CONFIG.get("investment_goal", {})
    risk = CONFIG.get("risk_control", {})
    paper = CONFIG.get("paper_mode", {})

    monthly_deposit = _cfg_float(goal, "monthly_deposit_jpy", 10_000)
    target_return = _cfg_float(goal, "target_monthly_return_pct", 15.0)
    risk_pct = _cfg_float(cap, "max_risk_per_trade_pct", 2.0)
    max_holdings = _cfg_int(cap, "max_concurrent_holdings", 3)
    rr_ratio = _cfg_float(sig, "risk_reward_ratio", 2.0)

    status_cards = [
        {"label": "毎月の入金", "value": _yen(monthly_deposit), "tone": "info"},
        {"label": "月次利益目標", "value": _pct(target_return), "tone": "warning"},
        {"label": "1トレード損失上限", "value": _pct(risk_pct), "tone": "danger"},
        {"label": "同時保有上限", "value": f"{max_holdings}銘柄", "tone": "success"},
    ]

    sections = [
        {
            "title": "買ってよい前提",
            "icon": "shield-check",
            "rules": [
                "市場環境が normal のときだけ新規買いを検討します。",
                "警戒モードでは候補を監視に回し、全体が弱い日は新規買いを止めます。",
                "スクリーニングは株価、売買代金、時価総額、直近30日の取引日数で足切りします。",
                f"時価総額が不明な銘柄を通す設定: {'有効' if _cfg_bool(screening, 'allow_unknown_market_cap', False) else '無効'}",
            ],
        },
        {
            "title": "シグナル条件",
            "icon": "activity",
            "rules": [
                f"パターンAは上昇トレンド、52週高値接近、出来高増、RSI {_cfg_float(sig, 'pattern_a_rsi_min', 50):.0f}-{_cfg_float(sig, 'pattern_a_rsi_max', 70):.0f} を確認します。",
                f"パターンBは上昇トレンド中の押し目、MA25反発、RSI {_cfg_float(sig, 'pattern_b_rsi_min', 40):.0f}-{_cfg_float(sig, 'pattern_b_rsi_max', 60):.0f} を確認します。",
                f"各パターンの通知上限は {_cfg_int(sig, 'max_candidates_per_pattern', 3)}件です。",
            ],
        },
        {
            "title": "資金管理",
            "icon": "wallet",
            "rules": [
                f"総資金は日本株 {_yen(_cfg_float(cap, 'total_capital_jpy', 200000))}、米国株 {_usd(_cfg_float(cap, 'total_capital_usd', 1500))} として計算します。",
                f"1回の失敗で失ってよい金額は資金の {_pct(risk_pct)} までです。",
                f"同時に持つ銘柄は最大 {max_holdings}銘柄です。",
                "損切り幅が狭すぎる、または広すぎる候補は見送ります。",
            ],
        },
        {
            "title": "出口ルール",
            "icon": "door-open",
            "rules": [
                f"損切りは ATR x {_cfg_float(sig, 'atr_stop_multiplier', 2.0):.1f} を基本にします。",
                f"利確目標は損切り幅に対して {rr_ratio:.1f}:1 のリスクリワードで置きます。",
                "損切りと利確の両方に触れた日は、保守的に損切りを優先して検証します。",
            ],
        },
        {
            "title": "検証ルール",
            "icon": "flask-conical",
            "rules": [
                f"ペーパートレードは {'有効' if _cfg_bool(paper, 'enabled', False) else '無効'} です。",
                "シグナル当日の終値ではなく、翌営業日の始値で仮想約定します。",
                "約定日の高値・安値では決済判定をしません。",
                f"仮想約定のスリッページは {_pct(_cfg_float(paper, 'entry_slippage_pct', 0.0))} です。",
            ],
        },
        {
            "title": "停止ルール",
            "icon": "octagon-alert",
            "rules": [
                f"月間損失が資金の {_pct(_cfg_float(risk, 'monthly_loss_stop_pct', 6.0))} に達したら新規買いを止めます。",
                f"連続損失が {_cfg_int(risk, 'max_consecutive_losses_before_pause', 3)}回に達したら新規買いを止めます。",
                "停止中でも記録と振り返りは続け、設定と実績を確認します。",
            ],
        },
    ]

    warnings = [
        "月15%は保証ではなく、毎月の検証目標として扱います。",
        "個別株は元本割れの可能性があります。実弾投入前にペーパートレードで期待値を確認してください。",
    ]
    return {"status_cards": status_cards, "sections": sections, "warnings": warnings}


def _cfg_float(section: dict, key: str, default: float) -> float:
    try:
        return float(section.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _cfg_int(section: dict, key: str, default: int) -> int:
    try:
        return int(section.get(key, default))
    except (TypeError, ValueError):
        return int(default)


def _cfg_bool(section: dict, key: str, default: bool) -> bool:
    value = section.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _yen(value: float) -> str:
    return f"¥{value:,.0f}"


def _usd(value: float) -> str:
    return f"${value:,.0f}"


def _pct(value: float) -> str:
    return f"{value:.1f}%"


@app.route("/paypay")
def paypay_page():
    """PayPay ポイント運用ページ"""
    from src.paypay_points import PAYPAY_COURSES, evaluate_paypay_points, generate_points_signals

    points = []
    signals = []
    try:
        points = evaluate_paypay_points()
        signals = generate_points_signals(date.today())
    except Exception as e:
        logger.warning(f"PayPayポイント評価エラー: {e}")

    # シグナルをコースキーで引けるように dict 化
    signal_map = {s["course"]: s for s in signals}

    total_current = sum(p["current_points"] for p in points)
    total_invested = sum(p["invested_points"] for p in points)
    total_return = total_current - total_invested
    total_pct = (total_return / total_invested * 100) if total_invested > 0 else 0.0

    # 利用可能なコース一覧（新規追加フォーム用）
    available_courses = [
        {"key": k, "name": v["name"]}
        for k, v in PAYPAY_COURSES.items()
    ]

    return render_template(
        "paypay.html",
        active_page="paypay",
        points=points,
        signal_map=signal_map,
        total_current=total_current,
        total_invested=total_invested,
        total_return=total_return,
        total_pct=total_pct,
        available_courses=available_courses,
    )


@app.route("/api/paypay-points", methods=["GET"])
def api_paypay_points_get():
    """PayPay ポイント運用の全コースデータを返す"""
    from src.paypay_points import PAYPAY_COURSES, evaluate_paypay_points
    try:
        points = evaluate_paypay_points()
        available = [{"key": k, "name": v["name"]} for k, v in PAYPAY_COURSES.items()]
        return jsonify({"success": True, "points": points, "available_courses": available})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/paypay-points", methods=["POST"])
def api_paypay_points_post():
    """PayPay ポイント運用の残高を更新する（CSV を書き換え → DB 同期）"""
    import csv
    from src.paypay_points import PAYPAY_COURSES, sync_paypay_points_from_csv

    data = request.get_json()
    if not data or "courses" not in data:
        return jsonify({"success": False, "error": "courses が必要です"}), 400

    courses = data["courses"]
    errors = []
    valid_rows = []

    for c in courses:
        course_key = c.get("course", "").strip()
        if not course_key:
            continue
        if course_key not in PAYPAY_COURSES:
            errors.append(f"不明なコース: {course_key}")
            continue
        try:
            invested = int(c.get("invested_points", 0))
            current = int(c.get("current_points", 0))
        except (ValueError, TypeError):
            errors.append(f"{course_key}: ポイント値が不正です")
            continue
        if invested < 0 or current < 0:
            errors.append(f"{course_key}: ポイントは0以上にしてください")
            continue
        last_updated = c.get("last_updated", "").strip() or date.today().isoformat()
        valid_rows.append({
            "course": course_key,
            "invested_points": invested,
            "current_points": current,
            "last_updated": last_updated,
        })

    if errors:
        return jsonify({"success": False, "error": "バリデーションエラー", "details": errors}), 400

    # CSV に書き出し
    csv_path = _PROJECT_ROOT / "paypay_points.csv"
    try:
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["course", "invested_points", "current_points", "last_updated"])
            writer.writeheader()
            writer.writerows(valid_rows)
        # DB に同期
        count = sync_paypay_points_from_csv()
        return jsonify({"success": True, "updated": count})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/paypay-points/delete", methods=["POST"])
def api_paypay_points_delete():
    """指定コースを削除する（CSV から除去 → DB 同期）"""
    import csv
    from src.paypay_points import sync_paypay_points_from_csv

    data = request.get_json()
    course_key = data.get("course", "").strip() if data else ""
    if not course_key:
        return jsonify({"success": False, "error": "course が必要です"}), 400

    csv_path = _PROJECT_ROOT / "paypay_points.csv"
    # 既存 CSV を読み込んで指定コースを除外して書き戻す
    remaining = []
    if csv_path.exists():
        with open(csv_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("course", "").strip() != course_key:
                    remaining.append(row)
    try:
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["course", "invested_points", "current_points", "last_updated"])
            writer.writeheader()
            writer.writerows(remaining)

        # DB からも削除
        with get_connection() as conn:
            conn.execute("DELETE FROM paypay_points WHERE course = ?", (course_key,))
            conn.commit()

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# 設定ページ
# ============================================================

_CONFIG_PATH = _PROJECT_ROOT / "config.json"


@app.route("/settings")
def settings_page():
    """設定ページ"""
    return render_template("settings.html", active_page="settings")


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    """現在の設定値を返す"""
    result = []
    for item in _EDITABLE_SETTINGS:
        section = CONFIG.get(item["section"], {})
        current = section.get(item["key"], item["default"])
        result.append({
            "section": item["section"],
            "key": item["key"],
            "label": item["label"],
            "type": item["type"],
            "value": current,
            "default": item["default"],
            "min": item["min"],
            "max": item["max"],
            "step": item["step"],
            "help": item["help"],
            "group": item["group"],
        })
    return jsonify({"settings": result, "group_labels": _GROUP_LABELS})


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    """設定値を更新して config.json に書き戻す"""
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "無効なリクエスト"}), 400

    updates = data.get("settings")
    if not isinstance(updates, dict):
        return jsonify({"error": "settings フィールドが必要です"}), 400

    # 編集可能なキーのセットを構築
    allowed = {(s["section"], s["key"]): s for s in _EDITABLE_SETTINGS}

    # バリデーション
    errors = []
    validated: list[tuple[str, str, object]] = []
    for compound_key, raw_value in updates.items():
        parts = compound_key.split(".", 1)
        if len(parts) != 2:
            errors.append(f"不正なキー形式: {compound_key}")
            continue
        section, key = parts
        spec = allowed.get((section, key))
        if spec is None:
            errors.append(f"編集不可のキー: {compound_key}")
            continue

        try:
            if spec["type"] == "bool":
                value = bool(raw_value)
            elif spec["type"] == "int":
                value = int(float(raw_value))
                if spec["min"] is not None and value < spec["min"]:
                    errors.append(f"{spec['label']}: 最小値は {spec['min']}")
                    continue
                if spec["max"] is not None and value > spec["max"]:
                    errors.append(f"{spec['label']}: 最大値は {spec['max']}")
                    continue
            elif spec["type"] == "float":
                value = round(float(raw_value), 4)
                if spec["min"] is not None and value < spec["min"]:
                    errors.append(f"{spec['label']}: 最小値は {spec['min']}")
                    continue
                if spec["max"] is not None and value > spec["max"]:
                    errors.append(f"{spec['label']}: 最大値は {spec['max']}")
                    continue
            else:
                errors.append(f"不明な型: {spec['type']}")
                continue
        except (ValueError, TypeError):
            errors.append(f"{spec['label']}: 値が不正です")
            continue

        validated.append((section, key, value))

    if errors:
        return jsonify({"error": "バリデーションエラー", "details": errors}), 400

    # config.json を読み直して更新（他セクションを壊さない）
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            full_config = json.load(f)
    except Exception as e:
        logger.error(f"config.json 読み込み失敗: {e}")
        return jsonify({"error": "config.json の読み込みに失敗しました"}), 500

    for section, key, value in validated:
        if section not in full_config:
            full_config[section] = {}
        full_config[section][key] = value

    # ファイルに書き戻し
    try:
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(full_config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"config.json 書き込み失敗: {e}")
        return jsonify({"error": "config.json の書き込みに失敗しました"}), 500

    # メモリ上の CONFIG も同期
    for section, key, value in validated:
        if section not in CONFIG:
            CONFIG[section] = {}
        CONFIG[section][key] = value

    logger.info(f"設定を更新しました: {len(validated)} 項目")
    return jsonify({"success": True, "updated": len(validated)})


# ============================================================
# プロンプトビルダー
# ============================================================


def _dedup_candidates(rows: list[dict]) -> list[dict]:
    """ティッカーごとに最良の行（final_score最大 → signal_date最新）のみ残す。"""
    best: dict[str, dict] = {}
    for row in rows:
        ticker = row["ticker"]
        if ticker not in best:
            best[ticker] = row
            continue
        prev = best[ticker]
        prev_score = prev.get("final_score") or 0
        curr_score = row.get("final_score") or 0
        if curr_score > prev_score or (
            curr_score == prev_score
            and (row.get("signal_date") or "") > (prev.get("signal_date") or "")
        ):
            best[ticker] = row
    return sorted(
        best.values(),
        key=lambda r: (r.get("final_score") or 0),
        reverse=True,
    )


@app.route("/prompt-builder")
def prompt_builder_page():
    """プロンプト作成ページ"""
    today_str = date.today().strftime("%Y-%m-%d")

    with get_connection() as conn:
        # 保有銘柄
        holdings_rows = fetch_all(
            conn,
            """
            SELECT h.ticker, h.name, h.market, h.entry_price, h.entry_date,
                   h.quantity, h.stop_loss_price,
                   (SELECT adj_close FROM price_history
                    WHERE ticker = h.ticker AND date <= ? AND is_holiday = 0
                    ORDER BY date DESC LIMIT 1) AS current_price,
                   (SELECT target_price FROM trade_signal_log
                    WHERE ticker = h.ticker ORDER BY signal_date DESC LIMIT 1) AS target_price,
                   t.rsi14 AS tech_rsi14, t.ma5 AS tech_ma5, t.ma25 AS tech_ma25,
                   t.ma75 AS tech_ma75, t.ma200 AS tech_ma200
            FROM holdings h
            LEFT JOIN technical_snapshot t
              ON t.ticker = h.ticker
              AND t.date = (SELECT MAX(date) FROM technical_snapshot WHERE ticker = h.ticker)
            WHERE h.is_closed = 0
            ORDER BY h.entry_date
            """,
            (today_str,),
        )

        # 直近7日の買い候補
        candidates_rows = fetch_all(
            conn,
            """
            SELECT tsl.ticker, tsl.name, tsl.market, tsl.pattern,
                   tsl.entry_price, tsl.stop_loss_price, tsl.stop_loss_pct,
                   tsl.target_price, tsl.recommended_amount, tsl.signal_date,
                   tsl.conditions_met,
                   ds.final_score, ds.recommend_rank,
                   ds.label AS ds_label,
                   ds.trend_score AS _trend_score,
                   ds.pattern_a_score AS _pattern_a_score,
                   ds.pattern_b_score AS _pattern_b_score,
                   ds.pattern_c_score AS _pattern_c_score,
                   ds.liquidity_score AS _liquidity_score,
                   t.rsi14 AS _rsi14, t.ma5 AS _ma5, t.ma25 AS _ma25,
                   t.ma75 AS _ma75, t.ma200 AS _ma200,
                   t.vol_20d_avg, t.high_52w AS _high_52w,
                   ph.adj_close AS _close,
                   CASE WHEN t.vol_20d_avg > 0
                        THEN ph.volume * 1.0 / t.vol_20d_avg
                        ELSE NULL END AS _vol_ratio
            FROM trade_signal_log tsl
            LEFT JOIN daily_signals ds
              ON ds.ticker = tsl.ticker AND ds.date = tsl.signal_date
            LEFT JOIN technical_snapshot t
              ON t.ticker = tsl.ticker
              AND t.date = (SELECT MAX(date) FROM technical_snapshot WHERE ticker = tsl.ticker)
            LEFT JOIN price_history ph
              ON ph.ticker = tsl.ticker
              AND ph.date = (SELECT MAX(date) FROM price_history WHERE ticker = tsl.ticker AND is_holiday = 0)
            WHERE tsl.signal_date >= date(?, '-7 days')
            ORDER BY tsl.signal_date DESC, tsl.ticker
            """,
            (today_str,),
        )

        # 市場環境
        market_env = fetch_one(
            conn,
            "SELECT * FROM market_environment ORDER BY date DESC LIMIT 1",
        )

    # 保有銘柄に損益率を計算して付加
    holdings = []
    for row in holdings_rows:
        h = dict(row)
        entry = h.get("entry_price") or 0
        current = h.get("current_price") or 0
        h["pnl_pct"] = ((current - entry) / entry * 100) if entry > 0 else 0
        if h.get("entry_date"):
            try:
                ed = date.fromisoformat(h["entry_date"])
                h["holding_days"] = (date.today() - ed).days
            except (ValueError, TypeError):
                h["holding_days"] = "?"
        else:
            h["holding_days"] = "?"
        h["label"] = "hold"  # 簡易判定
        if h.get("stop_loss_price") and current > 0:
            sl = h["stop_loss_price"]
            if current <= sl:
                h["label"] = "forced_stop"
            elif (current - sl) / current * 100 < 2.0:
                h["label"] = "sl_near"
        h["reasons"] = []
        holdings.append(h)

    candidates = _dedup_candidates([dict(r) for r in candidates_rows])
    me = dict(market_env) if market_env else {}

    from src.prompt_builder import THEMES
    return render_template(
        "prompt_builder.html",
        active_page="prompt_builder",
        holdings=holdings,
        candidates=candidates,
        market_env=me,
        themes=THEMES,
    )


@app.route("/api/prompt-builder/generate", methods=["POST"])
def api_prompt_builder_generate():
    """テーマに基づいてプロンプトを生成する"""
    from src.prompt_builder import generate_prompt

    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "リクエストボディが必要です"}), 400

    theme = data.get("theme", "")
    tickers = data.get("tickers", [])
    options = data.get("options", {})

    today_str = date.today().strftime("%Y-%m-%d")

    with get_connection() as conn:
        # 保有銘柄
        holdings_rows = fetch_all(
            conn,
            """
            SELECT h.ticker, h.name, h.market, h.entry_price, h.entry_date,
                   h.quantity, h.stop_loss_price,
                   (SELECT adj_close FROM price_history
                    WHERE ticker = h.ticker AND date <= ? AND is_holiday = 0
                    ORDER BY date DESC LIMIT 1) AS current_price,
                   (SELECT target_price FROM trade_signal_log
                    WHERE ticker = h.ticker ORDER BY signal_date DESC LIMIT 1) AS target_price,
                   t.rsi14 AS tech_rsi14, t.ma5 AS tech_ma5, t.ma25 AS tech_ma25,
                   t.ma75 AS tech_ma75, t.ma200 AS tech_ma200
            FROM holdings h
            LEFT JOIN technical_snapshot t
              ON t.ticker = h.ticker
              AND t.date = (SELECT MAX(date) FROM technical_snapshot WHERE ticker = h.ticker)
            WHERE h.is_closed = 0
            ORDER BY h.entry_date
            """,
            (today_str,),
        )

        candidates_rows = fetch_all(
            conn,
            """
            SELECT tsl.ticker, tsl.name, tsl.market, tsl.pattern,
                   tsl.entry_price, tsl.stop_loss_price, tsl.stop_loss_pct,
                   tsl.target_price, tsl.recommended_amount, tsl.signal_date,
                   tsl.conditions_met,
                   ds.final_score, ds.recommend_rank,
                   ds.label AS ds_label,
                   ds.trend_score AS _trend_score,
                   ds.pattern_a_score AS _pattern_a_score,
                   ds.pattern_b_score AS _pattern_b_score,
                   ds.pattern_c_score AS _pattern_c_score,
                   ds.liquidity_score AS _liquidity_score,
                   t.rsi14 AS _rsi14, t.ma5 AS _ma5, t.ma25 AS _ma25,
                   t.ma75 AS _ma75, t.ma200 AS _ma200,
                   t.vol_20d_avg, t.high_52w AS _high_52w,
                   ph.adj_close AS _close,
                   CASE WHEN t.vol_20d_avg > 0
                        THEN ph.volume * 1.0 / t.vol_20d_avg
                        ELSE NULL END AS _vol_ratio
            FROM trade_signal_log tsl
            LEFT JOIN daily_signals ds
              ON ds.ticker = tsl.ticker AND ds.date = tsl.signal_date
            LEFT JOIN technical_snapshot t
              ON t.ticker = tsl.ticker
              AND t.date = (SELECT MAX(date) FROM technical_snapshot WHERE ticker = tsl.ticker)
            LEFT JOIN price_history ph
              ON ph.ticker = tsl.ticker
              AND ph.date = (SELECT MAX(date) FROM price_history WHERE ticker = tsl.ticker AND is_holiday = 0)
            WHERE tsl.signal_date >= date(?, '-7 days')
            ORDER BY tsl.signal_date DESC, tsl.ticker
            """,
            (today_str,),
        )

        market_env = fetch_one(
            conn,
            "SELECT * FROM market_environment ORDER BY date DESC LIMIT 1",
        )

    # enrich holdings
    holdings = []
    for row in holdings_rows:
        h = dict(row)
        entry = h.get("entry_price") or 0
        current = h.get("current_price") or 0
        h["pnl_pct"] = ((current - entry) / entry * 100) if entry > 0 else 0
        if h.get("entry_date"):
            try:
                ed = date.fromisoformat(h["entry_date"])
                h["holding_days"] = (date.today() - ed).days
            except (ValueError, TypeError):
                h["holding_days"] = "?"
        else:
            h["holding_days"] = "?"
        h["label"] = "hold"
        if h.get("stop_loss_price") and current > 0:
            sl = h["stop_loss_price"]
            if current <= sl:
                h["label"] = "forced_stop"
            elif (current - sl) / current * 100 < 2.0:
                h["label"] = "sl_near"
        h["reasons"] = []
        holdings.append(h)

    candidates = _dedup_candidates([dict(r) for r in candidates_rows])
    me = dict(market_env) if market_env else {}

    result = generate_prompt(theme, tickers, holdings, candidates, me, options)
    return jsonify({"success": True, **result})


# ============================================================
# バッチ手動実行
# ============================================================

@dataclass
class _BatchTask:
    batch_type: str
    status: str = "running"  # running / success / partial / failed / locked / timeout
    started_at: str = ""
    finished_at: str = ""
    return_code: int | None = None
    error: str = ""
    current_step: int = 0
    total_steps: int = 0
    step_label: str = ""


_batch_tasks: dict[str, _BatchTask] = {}
_batch_lock = threading.Lock()

_RC_STATUS = {0: "success", 1: "partial", 2: "failed", 3: "locked", 4: "timeout"}


def _run_batch_async(batch_type: str) -> bool:
    """バッチをバックグラウンドスレッドで起動する。成功時 True。"""
    with _batch_lock:
        existing = _batch_tasks.get(batch_type)
        if existing and existing.status == "running":
            return False
        task = _BatchTask(
            batch_type=batch_type,
            status="running",
            started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        _batch_tasks[batch_type] = task

    def _on_progress(step: int, total: int, label: str) -> None:
        with _batch_lock:
            task.current_step = step
            task.total_steps = total
            task.step_label = label

    def _worker():
        try:
            if batch_type == "evening_jp":
                from scripts.batch_evening_jp import run_manual
            else:
                from scripts.batch_morning_all import run_manual
            rc = run_manual(skip_llm=True, on_progress=_on_progress)
            with _batch_lock:
                task.return_code = rc
                task.status = _RC_STATUS.get(rc, "failed")
                task.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        except Exception as e:
            logger.error(f"バッチ手動実行エラー ({batch_type}): {e}", exc_info=True)
            with _batch_lock:
                task.status = "failed"
                task.error = str(e)
                task.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    thread = threading.Thread(target=_worker, name=f"batch-{batch_type}", daemon=True)
    thread.start()
    return True


@app.route("/api/batch/trigger", methods=["POST"])
def api_batch_trigger():
    """バッチ手動実行をトリガーする"""
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "無効なリクエスト"}), 400

    batch_type = data.get("batch_type", "")
    if batch_type not in ("evening_jp", "morning_all"):
        return jsonify({"error": "無効なバッチタイプ"}), 400

    with _batch_lock:
        existing = _batch_tasks.get(batch_type)
        if existing and existing.status == "running":
            return jsonify({"error": "既に実行中です", "status": "running"}), 409

    ok = _run_batch_async(batch_type)
    if not ok:
        return jsonify({"error": "既に実行中です", "status": "running"}), 409

    return jsonify({"success": True, "batch_type": batch_type, "status": "running"})


@app.route("/api/batch/status")
def api_batch_status():
    """バッチ実行状態を返す（ポーリング用）"""
    batch_type = request.args.get("batch_type", "")

    with _batch_lock:
        if batch_type:
            task = _batch_tasks.get(batch_type)
            if not task:
                return jsonify({"batch_type": batch_type, "status": "idle"})
            return jsonify({
                "batch_type": task.batch_type,
                "status": task.status,
                "started_at": task.started_at,
                "finished_at": task.finished_at,
                "return_code": task.return_code,
                "error": task.error,
                "current_step": task.current_step,
                "total_steps": task.total_steps,
                "step_label": task.step_label,
            })
        else:
            result = {}
            for bt, task in _batch_tasks.items():
                result[bt] = {
                    "status": task.status,
                    "started_at": task.started_at,
                    "finished_at": task.finished_at,
                    "return_code": task.return_code,
                    "current_step": task.current_step,
                    "total_steps": task.total_steps,
                    "step_label": task.step_label,
                }
            return jsonify(result)


# ============================================================
# エントリーポイント
# ============================================================

def main():
    logger.info(f"レポートサーバーを起動します: http://{_HOST}:{_PORT}/report")
    logger.info("  /report       - メインダッシュボード")
    logger.info("  /screening    - スクリーニング銘柄一覧")
    logger.info("  /performance  - パフォーマンス分析")
    logger.info("  /paypay       - PayPayポイント運用")
    logger.info("  /prompt-builder - プロンプト作成")
    logger.info("  /settings     - 設定")
    logger.info("  /stock/<ticker> - 個別銘柄詳細")
    logger.info("  Ctrl+C で停止")
    app.run(host=_HOST, port=_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
