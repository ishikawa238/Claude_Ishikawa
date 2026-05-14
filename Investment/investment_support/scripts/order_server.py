"""
日次レポート Web サーバー (Phase 1-3 統合版)

Slack 通知のレポートリンクをクリック → ブラウザで保有銘柄・シグナル履歴・市場環境を確認する。

エンドポイント:
  /health              - ヘルスチェック
  /report              - メインダッシュボード
  /screening           - スクリーニング銘柄一覧
  /stock/<ticker>      - 個別銘柄詳細ページ
  /performance         - トレード成績分析ページ
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

実行方法（investment_support/ から）:
    python -m scripts.order_server
"""

import json
import math
import sys
import time as _time
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
_MODE_COLOR = {"normal": "#4CAF50", "caution": "#FF9800", "full_caution": "#D32F2F"}
_PATTERN_NAME = {"A": "モメンタムブレイクアウト", "B": "押し目買い"}
_RANK_COLOR = {"S": "#D32F2F", "A": "#FF5722", "B": "#FF9800", "C": "#2196F3", "Watch": "#9E9E9E"}
_LABEL_COLOR = {"candidate": "#4CAF50", "watch": "#FF9800", "skip": "#9E9E9E"}
_LABEL_DISPLAY = {"candidate": "候補", "watch": "監視", "skip": "スキップ"}
_PROFIT_TYPE_LABEL = {"I": "ブレイクアウト", "II": "トレンドフォロー", "III": "手動管理"}


def _build_tp_target_html(h: dict, cp: float) -> str:
    """保有銘柄の利確ターゲットをHTML文字列で構築する。"""
    parts = []
    for stage, label in [("t1", "T1"), ("t2", "T2"), ("t3", "T3")]:
        price = h.get(f"{stage}_price")
        reached = h.get(f"{stage}_reached")
        if price is None:
            if stage == "t3" and h.get("profit_type") == "II":
                if reached:
                    parts.append(f"<span style='color:#4CAF50'>&#10003; {label} MA25</span>")
                elif h.get("trailing_active"):
                    parts.append(f"<span style='font-weight:600'>&#8594; {label} MA25</span>")
                else:
                    parts.append(f"<span style='color:#999'>{label} MA25</span>")
            continue
        if reached:
            parts.append(f"<span style='color:#4CAF50'>&#10003; {label} {price:,.0f}</span>")
        elif cp >= price:
            dist = (price - cp) / cp * 100
            parts.append(f"<span style='font-weight:600;color:#FF6F00'>&#8594; {label} {price:,.0f}</span>")
        else:
            dist = (price - cp) / cp * 100
            parts.append(f"<span style='color:#999'>{label} {price:,.0f}（{dist:+.1f}%）</span>")
    return "<br>".join(parts) if parts else "—"
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
                   d.trend_score, d.pattern_a_score, d.pattern_b_score, d.liquidity_score,
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
        # 惜しかった銘柄（watch ラベル, スコア30以上）
        _latest_sig_date = "(SELECT MAX(date) FROM daily_signals)"
        near_misses = fetch_all(
            conn,
            f"""
            SELECT d.ticker, u.name, d.market, d.pattern, d.label,
                   d.cond_a_ma200_rising, d.cond_a_ma_aligned,
                   d.cond_a_52w_high_near, d.cond_a_volume_surge,
                   d.cond_a_rsi_ok, d.cond_a_above_ma25,
                   d.cond_b_ma200_rising, d.cond_b_ma_aligned,
                   d.cond_b_pullback_touched, d.cond_b_bounced,
                   d.cond_b_ma25_rising, d.cond_b_rsi_ok,
                   d.close, d.rsi14, d.atr14, d.vol_ratio,
                   d.final_score, d.recommend_rank, d.recommend_reason,
                   d.trend_score, d.pattern_a_score, d.pattern_b_score, d.liquidity_score,
                   d.market_mode,
                   CASE
                     WHEN d.label = 'candidate' THEN 'filtered'
                     WHEN d.market_mode IN ('caution', 'full_caution') AND d.final_score >= 50
                          THEN 'demoted'
                     ELSE 'score_short'
                   END AS near_miss_type
            FROM daily_signals d
            LEFT JOIN universe_master u ON u.ticker = d.ticker
            WHERE d.date = {_latest_sig_date}
              AND (
                (d.label = 'candidate'
                 AND d.ticker NOT IN (
                   SELECT ticker FROM trade_signal_log
                   WHERE signal_date = {_latest_sig_date}
                 ))
                OR
                (d.label = 'watch' AND d.final_score >= 30)
              )
            ORDER BY d.final_score DESC
            LIMIT 5
            """,
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

    return _render_report(today_str, market_env, holdings, candidates, market_history, llm_feedback, near_misses, last_batch, data_insufficient)


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
            """SELECT trend_score, pattern_a_score, pattern_b_score,
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
    with get_connection() as conn:
        # サマリー統計
        summary = fetch_one(
            conn,
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN pnl_pct <= 0 THEN 1 ELSE 0 END) AS losses,
                      AVG(pnl_pct) AS avg_pnl,
                      MIN(pnl_pct) AS worst_trade,
                      MAX(pnl_pct) AS best_trade,
                      SUM(CASE WHEN pnl_pct > 0 THEN pnl_pct ELSE 0 END) AS total_profit,
                      SUM(CASE WHEN pnl_pct < 0 THEN ABS(pnl_pct) ELSE 0 END) AS total_loss
               FROM trade_result_log
               WHERE actual_exit_date IS NOT NULL""",
        )
        summary = dict(summary) if summary else {}

        # パターン別
        pattern_stats = fetch_all(
            conn,
            """SELECT tsl.pattern,
                      COUNT(*) AS total,
                      SUM(CASE WHEN trl.pnl_pct > 0 THEN 1 ELSE 0 END) AS wins,
                      AVG(trl.pnl_pct) AS avg_pnl
               FROM trade_result_log trl
               JOIN trade_signal_log tsl ON tsl.id = trl.signal_id
               WHERE trl.actual_exit_date IS NOT NULL
               GROUP BY tsl.pattern""",
        )

        # 全トレード一覧（累積損益計算用）
        all_trades = fetch_all(
            conn,
            """SELECT trl.actual_exit_date, trl.pnl_pct, trl.pnl_amount,
                      trl.ticker, trl.exit_reason, tsl.pattern
               FROM trade_result_log trl
               JOIN trade_signal_log tsl ON tsl.id = trl.signal_id
               WHERE trl.actual_exit_date IS NOT NULL
               ORDER BY trl.actual_exit_date""",
        )

    return _render_performance(summary, pattern_stats, all_trades)


# ============================================================
# ルーティング: /screening  (スクリーニング銘柄一覧)
# ============================================================

@app.route("/screening")
def screening():
    """スクリーニング銘柄一覧ページ"""
    with get_connection() as conn:
        latest = fetch_one(conn, "SELECT MAX(date) AS d FROM daily_signals")
        target_date = dict(latest)["d"] if latest else None
        if not target_date:
            return "<h3>スクリーニングデータがありません</h3>", 404

        rows = fetch_all(
            conn,
            """SELECT d.ticker, u.name, u.sector, d.market, d.pattern, d.label,
                      d.cond_a_ma200_rising, d.cond_a_ma_aligned,
                      d.cond_a_52w_high_near, d.cond_a_volume_surge,
                      d.cond_a_rsi_ok, d.cond_a_above_ma25,
                      d.cond_b_ma200_rising, d.cond_b_ma_aligned,
                      d.cond_b_pullback_touched, d.cond_b_bounced,
                      d.cond_b_ma25_rising, d.cond_b_rsi_ok,
                      d.close, d.rsi14, d.atr14, d.vol_ratio, d.rs_rank,
                      d.final_score, d.recommend_rank, d.recommend_reason
               FROM daily_signals d
               LEFT JOIN universe_master u ON u.ticker = d.ticker
               WHERE d.date = ?
               ORDER BY d.final_score DESC""",
            (target_date,),
        )

    return _render_screening(target_date, [dict(r) for r in rows])


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
            """SELECT trend_score, pattern_a_score, pattern_b_score, liquidity_score
               FROM daily_signals WHERE ticker = ?
               ORDER BY date DESC LIMIT 1""",
            (ticker,),
        )

    if not row:
        return jsonify({"trend": 0, "pattern_a": 0, "pattern_b": 0, "liquidity": 0})

    row = dict(row)
    return jsonify({
        "trend": row.get("trend_score") or 0,
        "pattern_a": row.get("pattern_a_score") or 0,
        "pattern_b": row.get("pattern_b_score") or 0,
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
    with get_connection() as conn:
        rows = fetch_all(
            conn,
            """SELECT strftime('%Y-%m', actual_exit_date) AS month,
                      SUM(pnl_pct) AS total_pnl,
                      COUNT(*) AS trades,
                      SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) AS wins
               FROM trade_result_log
               WHERE actual_exit_date IS NOT NULL
               GROUP BY month ORDER BY month""",
        )

    rows = [dict(r) for r in rows]
    return jsonify({
        "months": [r["month"] for r in rows],
        "pnl": [round(r["total_pnl"] or 0, 2) for r in rows],
        "trades": [r["trades"] for r in rows],
        "wins": [r["wins"] or 0 for r in rows],
    })


@app.route("/api/perf-cumulative")
def api_perf_cumulative():
    """累積損益データ"""
    with get_connection() as conn:
        rows = fetch_all(
            conn,
            """SELECT actual_exit_date, pnl_pct
               FROM trade_result_log
               WHERE actual_exit_date IS NOT NULL
               ORDER BY actual_exit_date""",
        )

    dates = []
    cum = []
    running = 0.0
    for r in rows:
        r = dict(r)
        running += float(r["pnl_pct"] or 0)
        dates.append(r["actual_exit_date"])
        cum.append(round(running, 2))

    return jsonify({"dates": dates, "cumulative_pnl": cum})


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


@app.route("/api/screening-funnel")
def api_screening_funnel():
    """スクリーニングファネル統計（直近バッチ分）"""
    target = request.args.get("date")

    with get_connection() as conn:
        if not target:
            row = fetch_one(conn, "SELECT MAX(date) AS d FROM daily_signals")
            target = dict(row)["d"] if row else None
        if not target:
            return jsonify({"error": "no data"}), 404

        universe = fetch_one(
            conn,
            "SELECT COUNT(*) AS cnt FROM universe_master "
            "WHERE is_active = 1 AND is_excluded = 0",
        )
        universe_cnt = dict(universe)["cnt"] if universe else 0

        screened = fetch_one(
            conn,
            "SELECT COUNT(*) AS cnt FROM daily_signals WHERE date = ?",
            (target,),
        )
        screened_cnt = dict(screened)["cnt"] if screened else 0

        label_counts = fetch_all(
            conn,
            "SELECT label, COUNT(*) AS cnt FROM daily_signals "
            "WHERE date = ? GROUP BY label",
            (target,),
        )
        labels = {dict(r)["label"]: dict(r)["cnt"] for r in label_counts}

        notified = fetch_one(
            conn,
            "SELECT COUNT(*) AS cnt FROM trade_signal_log WHERE signal_date = ?",
            (target,),
        )
        notified_cnt = dict(notified)["cnt"] if notified else 0

        by_market_rows = fetch_all(
            conn,
            "SELECT market, label, COUNT(*) AS cnt FROM daily_signals "
            "WHERE date = ? GROUP BY market, label",
            (target,),
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
            """SELECT COUNT(*) AS total,
                      SUM(cond_a_ma200_rising)  AS c1, SUM(cond_a_ma_aligned)    AS c2,
                      SUM(cond_a_52w_high_near) AS c3, SUM(cond_a_volume_surge)  AS c4,
                      SUM(cond_a_rsi_ok)        AS c5, SUM(cond_a_above_ma25)    AS c6
               FROM daily_signals
               WHERE date = ? AND cond_a_ma200_rising IS NOT NULL""",
            (target,),
        )
        cond_a = dict(cond_a) if cond_a else {}

        cond_b = fetch_one(
            conn,
            """SELECT COUNT(*) AS total,
                      SUM(cond_b_ma200_rising)     AS c1, SUM(cond_b_ma_aligned)       AS c2,
                      SUM(cond_b_pullback_touched) AS c3, SUM(cond_b_bounced)          AS c4,
                      SUM(cond_b_ma25_rising)      AS c5, SUM(cond_b_rsi_ok)           AS c6
               FROM daily_signals
               WHERE date = ? AND cond_b_ma200_rising IS NOT NULL""",
            (target,),
        )
        cond_b = dict(cond_b) if cond_b else {}

        score_dist = fetch_all(
            conn,
            """SELECT
                 CASE WHEN final_score >= 80 THEN '80-100'
                      WHEN final_score >= 60 THEN '60-79'
                      WHEN final_score >= 40 THEN '40-59'
                      WHEN final_score >= 20 THEN '20-39'
                      ELSE '0-19' END AS bucket,
                 COUNT(*) AS cnt
               FROM daily_signals WHERE date = ? AND final_score IS NOT NULL
               GROUP BY bucket ORDER BY bucket DESC""",
            (target,),
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

def _render_report(today_str, market_env, holdings, candidates, market_history, llm_feedback=None, near_misses=None, last_batch=None, data_insufficient=0):
    market_env = dict(market_env) if market_env else {}
    holdings = [dict(h) for h in holdings]
    candidates = [dict(c) for c in candidates]
    market_history = [dict(m) for m in market_history]
    llm_feedback = [dict(f) for f in llm_feedback] if llm_feedback else []

    mode = market_env.get("mode", "unknown")
    mode_label = _MODE_LABEL.get(mode, mode)
    mode_color = _MODE_COLOR.get(mode, "#999")

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
    no_price_warn = (
        f'<div style="font-size:0.7rem;color:#E65100;margin-top:4px">'
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
        freshness_html = f'<div style="font-size:0.75rem;color:#666">最終バッチ: {batch_label} {batch_status_mark} {escape(str(batch_time))}</div>'
    else:
        freshness_html = '<div style="font-size:0.75rem;color:#E65100">⚠ バッチ実行履歴なし</div>'

    # --- 今日の行動セクション構築 ---
    action_items = []
    for h in holdings:
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
            _tp_items = []
            for stage, label_str in [("t1", "T1"), ("t2", "T2"), ("t3", "T3")]:
                t_price = h.get(f"{stage}_price")
                t_reached = h.get(f"{stage}_reached")
                if t_price and not t_reached and cp >= t_price:
                    _tp_items.append(label_str)
            if _tp_items:
                _qty = h.get("quantity") or 1
                action_items.append((
                    "info",
                    f"🎯 {h['ticker']} {h.get('name', '')} — "
                    f"{'・'.join(_tp_items)}到達。利確を検討してください"
                ))
            # SL引き上げ推奨
            _adj = h.get("adjusted_stop")
            if _adj and _adj > sl:
                action_items.append((
                    "info",
                    f"📋 {h['ticker']} — 損切りライン引き上げ推奨: "
                    f"{sl:,.2f} → {_adj:,.2f}（買値付近）"
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
    for h in holdings:
        ep = float(h["entry_price"] or 0)
        cp = float(h["current_price"] or 0) if h["current_price"] else None
        sl = float(h["stop_loss_price"] or 0)
        tp = float(h["target_price"] or 0) if h["target_price"] else None
        qty = h["quantity"]

        pnl_pct = 0.0
        pnl_str = "—"
        pnl_color = ""
        if cp and ep:
            pnl_pct = (cp - ep) / ep * 100
            pnl_color = "color:#4CAF50" if pnl_pct >= 0 else "color:#D32F2F"
            pnl_str = f"{pnl_pct:+.1f}%"

        cp_str = f"{cp:,.2f}" if cp else "—"

        # 利確ターゲット表示の構築
        profit_type = h.get("profit_type")
        target_str = "—"
        target_html = "—"
        if profit_type in ("I", "II") and cp:
            target_html = _build_tp_target_html(h, cp)
        elif tp and cp:
            dist = (tp - cp) / cp * 100
            target_html = f"{tp:,.2f}（{dist:+.1f}%）"
        target_str = target_html

        days_str = "—"
        if h["entry_date"]:
            try:
                entry_dt = datetime.strptime(h["entry_date"], "%Y-%m-%d").date()
                days_str = f"{(date.today() - entry_dt).days}日"
            except ValueError:
                pass

        # テクニカル指標
        rsi = h.get("rsi14")
        rsi_str = f"{rsi:.0f}" if rsi is not None else "—"
        rsi_color = ""
        if rsi is not None:
            if rsi >= 70:
                rsi_color = "color:#D32F2F"
            elif rsi <= 30:
                rsi_color = "color:#4CAF50"

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
        rank_color = _RANK_COLOR.get(frank, "#999")
        rank_html = f"<span class='rank-badge' style='background:{rank_color}'>{escape(frank or '—')}</span>"

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
            "pnl_color": pnl_color,
            "pnl_str": pnl_str,
            "status_cls": status_cls,
            "status_label": status_label,
            "sl_str": f"{sl:,.2f}",
            "target_str": target_str,
            "days_str": days_str,
            "market": h.get("market") or "",
            "qty_str": f"{qty:.0f}" if qty else "—",
            "rsi_color": rsi_color,
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

    # 惜しかった銘柄カードリスト構築（card_html を pre-render）
    near_misses = [dict(nm) for nm in near_misses] if near_misses else []
    _NM_TYPE_BADGE = {
        "filtered": ("🚫 見送り", "#D32F2F"),
        "demoted":  ("⚠️ 警戒モードで降格", "#FF5722"),
        "score_short": ("", "#FF9800"),
    }
    near_miss_cards = []
    for nm in near_misses:
        nm_ticker = escape(nm.get("ticker") or "")
        nm_name = escape(nm.get("name") or str(nm_ticker))
        nm_market = escape(nm.get("market") or "")
        nm_pattern = nm.get("pattern") or "A"
        nm_p_name = escape(_PATTERN_NAME.get(nm_pattern, nm_pattern))
        nm_score = int(nm.get("final_score") or 0)
        nm_type = nm.get("near_miss_type", "score_short")
        _, nm_border_color = _NM_TYPE_BADGE.get(nm_type, ("", "#FF9800"))

        # --- filtered タイプ: 具体的な見送り理由を再計算 ---
        skip_detail_html = ""
        if nm_type == "filtered":
            _nm_close = float(nm.get("close") or 0)
            _nm_atr = float(nm.get("atr14") or 0)
            if _nm_close > 0 and _nm_atr > 0:
                _nm_sl = _nm_close - _nm_atr * _SKIP_ATR_MULT
                _nm_sl_pct = (_nm_close - _nm_sl) / _nm_close * 100
                # filtered の見送り理由は「損切り幅が狭すぎる」のみ
                skip_detail_html = (
                    f'<div style="background:#FFEBEE;border-radius:8px;padding:8px 12px;margin-bottom:8px;font-size:0.8rem">'
                    f'<strong>🚫 見送り理由: 損切り幅が狭すぎる</strong><br>'
                    f'損切り幅 <strong>{_nm_sl_pct:.2f}%</strong> ＜ 最低ライン <strong>{_SKIP_SL_MIN_PCT}%</strong><br>'
                    f'<span style="color:#888">終値 {_nm_close:,.0f} / ATR14 {_nm_atr:,.1f} / '
                    f'SL計算値 {_nm_sl:,.0f}（ATR×{_SKIP_ATR_MULT:.0f}）</span>'
                    f'</div>'
                )

        # スコア表示: typeに応じて変更
        if nm_type == "score_short":
            nm_gap = 50 - nm_score
            nm_gap_str = f"あと{nm_gap}点"
        elif nm_type == "filtered":
            nm_gap_str = "🚫 見送り"
        else:
            nm_gap_str = _NM_TYPE_BADGE.get(nm_type, ("", "#FF9800"))[0]

        nm_pct = min(nm_score, 100)  # 0-100 でバー表示
        nm_reason = escape(nm.get("recommend_reason") or "")

        # サブスコアミニバー
        sub_bars = ""
        for sub_key, sub_label in [("trend_score", "トレンド"), ("pattern_a_score", "ブレイクアウト"),
                                    ("pattern_b_score", "押し目"), ("liquidity_score", "流動性")]:
            sv = int(nm.get(sub_key) or 0)
            sc = "#4CAF50" if sv >= 60 else "#FF9800" if sv >= 30 else "#E0E0E0"
            sub_bars += f"""<div class="col-3">
              <div style="font-size:0.7rem;color:#666">{sub_label}</div>
              <div style="background:#f0f0f0;border-radius:4px;height:8px">
                <div style="background:{sc};width:{sv}%;height:100%;border-radius:4px"></div>
              </div>
              <span style="font-size:0.7rem">{sv}点</span>
            </div>"""

        # 条件チェックリスト
        cond_labels = _COND_A_LABELS if nm_pattern == "A" else _COND_B_LABELS
        cond_rows = ""
        for col, desc in cond_labels:
            val = nm.get(col)
            if val == 1:
                dot = "#4CAF50"
                hint = ""
            elif val == 0:
                dot = "#D32F2F"
                hint = "<span style='color:#D32F2F;font-size:0.7rem;margin-left:4px'>← 未達</span>"
            else:
                dot = "#E0E0E0"
                hint = ""
            cond_rows += f"""<div style="display:flex;align-items:center;gap:4px;margin-bottom:2px">
              <span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:{dot}"></span>
              <span style="font-size:0.8rem">{escape(desc)}</span>{hint}
            </div>"""

        card_html = f"""
        <div class="summary-card" style="border-left:4px solid {nm_border_color};margin-bottom:12px">
          <div class="d-flex align-items-center gap-2 mb-2">
            <a class="ticker-link" href="/stock/{nm_ticker}">{nm_ticker}</a>
            <span>{nm_name}</span>
            <span style="font-size:0.75rem;color:#999">{nm_market}</span>
            <span class="rank-badge" style="background:#FF9800">{nm_p_name}</span>
            <span style="margin-left:auto;font-weight:700;font-size:1.1rem">{nm_score}点</span>
            <span style="color:{nm_border_color};font-size:0.8rem">{nm_gap_str}</span>
          </div>
          {skip_detail_html}
          <div style="background:#f0f0f0;border-radius:8px;height:20px;margin-bottom:8px;position:relative">
            <div style="background:linear-gradient(90deg,#FF9800,#FFC107);width:{nm_pct}%;height:100%;border-radius:8px"></div>
            <span style="position:absolute;right:8px;top:2px;font-size:0.7rem;color:#666">50点ライン</span>
          </div>
          <div class="row g-2 mb-2">{sub_bars}</div>
          <div style="margin-bottom:4px">{cond_rows}</div>
          <div style="font-size:0.8rem;color:#666">💡 {nm_reason}</div>
        </div>"""
        near_miss_cards.append({"card_html": card_html})

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
        mode_color=mode_color,
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
        near_miss_cards=near_miss_cards,
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
    rsi_bar_color = "#D32F2F" if rsi and rsi >= 70 else ("#4CAF50" if rsi and rsi <= 30 else "#1976D2")
    rsi_width = rsi if rsi is not None else 0

    vol = tech.get("vol_20d_avg")
    vol_str = f"{vol:,.0f}" if vol else "—"

    # スコア
    fscore = score.get("final_score")
    frank = score.get("recommend_rank") or "—"
    reason = score.get("recommend_reason") or "—"
    rank_color = _RANK_COLOR.get(frank, "#999")

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
        rank_color=rank_color,
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

def _render_performance(summary, pattern_stats, all_trades):
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

    # パターン別リスト
    pat_rows = []
    for p in pattern_stats:
        pat_name = _PATTERN_NAME.get(p.get("pattern", ""), p.get("pattern", ""))
        pt = p.get("total") or 0
        pw = p.get("wins") or 0
        pa = p.get("avg_pnl") or 0
        pwr = (pw / pt * 100) if pt > 0 else 0
        pat_rows.append({
            "pattern_name": pat_name,
            "total": pt,
            "wins": pw,
            "win_rate": pwr,
            "avg_pnl": pa,
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
        trade_rows.append({
            "exit_date": t.get("actual_exit_date") or "",
            "ticker": t.get("ticker") or "",
            "pattern_name": pat_name,
            "pnl": pnl,
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
        pat_rows=pat_rows,
        trade_rows=trade_rows,
    )


# ============================================================
# HTML レンダリング: /screening
# ============================================================

def _cond_dot(value: int | None, label: str) -> str:
    """条件フラグを色付きドットで描画する。"""
    color = "#4CAF50" if value else "#E0E0E0"
    return (
        f'<span class="tip" data-tip="{label}" '
        f'style="display:inline-block;width:14px;height:14px;'
        f'border-radius:50%;background:{color};margin:0 1px"></span>'
    )


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
        lbl_color = _LABEL_COLOR.get(label, "#9E9E9E")
        lbl_text = _LABEL_DISPLAY.get(label, label)

        # ランクバッジ
        rank_color = _RANK_COLOR.get(frank, "#999")

        # 条件フラグドット
        conds_html = ""
        cond_labels = _COND_A_LABELS if pattern == "A" else _COND_B_LABELS
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
            verdict_html = '<span style="color:#4CAF50;font-weight:600">買い候補</span>'
        elif label == "watch":
            verdict_html = '<span style="color:#FF9800;font-weight:600">監視</span>'
        else:
            verdict_html = '<span style="color:#9E9E9E">対象外</span>'
        # 未達条件を赤で表示
        if failed_names:
            failed_html = ", ".join(
                f'<span style="color:#D32F2F">{escape(n)}</span>' for n in failed_names
            )
            verdict_detail = f'{verdict_html} ({met_count}/{total_conds})<br><span style="font-size:0.75rem">未達: {failed_html}</span>'
        else:
            verdict_detail = f'{verdict_html} ({met_count}/{total_conds})'
        # 理由
        reason_html = f'<span style="font-size:0.75rem;color:#666">{escape(reason_text)}</span>' if reason_text else "—"

        # 詳細列
        close_str = f"{close:,.2f}" if close else "—"
        rsi_str = f"{rsi:.0f}" if rsi is not None else "—"
        rsi_color = ""
        if rsi is not None:
            if rsi >= 70:
                rsi_color = "color:#D32F2F"
            elif rsi <= 30:
                rsi_color = "color:#4CAF50"
        vr_str = f"{vol_ratio:.1f}x" if vol_ratio is not None else "—"

        row_html = (
            f'<tr data-label="{escape(label)}" data-market="{escape(market)}"'
            f' data-pattern="{escape(pattern)}" data-score="{fscore or 0}">'
            f'<td><a class="ticker-link" href="/stock/{escape(ticker)}">{escape(ticker)}</a></td>'
            f'<td class="mobile-hide">{escape(name)}</td>'
            f'<td class="mobile-hide">{escape(market)}</td>'
            f"<td>{pat_html}</td>"
            f'<td><span class="rank-badge" style="background:{lbl_color}">{lbl_text}</span></td>'
            f'<td class="mobile-hide" style="white-space:nowrap">{conds_html}</td>'
            f"<td>{score_str}</td>"
            f'<td><span class="rank-badge" style="background:{rank_color}">{escape(frank)}</span></td>'
            f'<td class="mobile-hide">{verdict_detail}</td>'
            f'<td class="mobile-hide">{reason_html}</td>'
            f'<td class="detail-col">{close_str}</td>'
            f'<td class="detail-col" style="{rsi_color}">{rsi_str}</td>'
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
# エントリーポイント
# ============================================================

def main():
    logger.info(f"レポートサーバーを起動します: http://{_HOST}:{_PORT}/report")
    logger.info("  /report       - メインダッシュボード")
    logger.info("  /screening    - スクリーニング銘柄一覧")
    logger.info("  /performance  - パフォーマンス分析")
    logger.info("  /stock/<ticker> - 個別銘柄詳細")
    logger.info("  Ctrl+C で停止")
    app.run(host=_HOST, port=_PORT, debug=False)


if __name__ == "__main__":
    main()
