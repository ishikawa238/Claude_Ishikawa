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

実行方法（investment_support/ から）:
    python -m scripts.order_server
"""

import json
import math
import sys
from datetime import date, datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent  # Ishikawa238/ — shared パッケージの検索に必要
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

from flask import Flask, request, jsonify
from markupsafe import escape

from src.config import CONFIG
from src.data_store import fetch_all, fetch_one, get_connection
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

app = Flask(__name__)

# ============================================================
# 定数
# ============================================================

_MODE_LABEL = {"normal": "通常モード", "caution": "警戒モード", "full_caution": "全面警戒モード"}
_MODE_COLOR = {"normal": "#4CAF50", "caution": "#FF9800", "full_caution": "#D32F2F"}
_PATTERN_NAME = {"A": "モメンタムブレイクアウト", "B": "押し目買い"}
_RANK_COLOR = {"S": "#D32F2F", "A": "#FF5722", "B": "#FF9800", "C": "#2196F3", "Watch": "#9E9E9E"}
_LABEL_COLOR = {"candidate": "#4CAF50", "watch": "#FF9800", "skip": "#9E9E9E"}
_LABEL_DISPLAY = {"candidate": "候補", "watch": "監視", "skip": "スキップ"}
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
# 共通 HTML パーツ
# ============================================================

def _common_head(title: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0"></script>
<style>
  body {{ padding: 24px; font-size: 14px; background: #f8f9fa; }}
  h2 {{ font-size: 1.1rem; font-weight: 600; margin-bottom: 12px; }}
  .mode-badge {{ display: inline-block; padding: 4px 12px; border-radius: 20px;
                 color: #fff; font-weight: 600; }}
  .rank-badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px;
                 color: #fff; font-weight: 600; font-size: 0.8rem; }}
  th {{ white-space: nowrap; }}
  .section {{ margin-bottom: 32px; }}
  .summary-card {{ border-radius: 12px; padding: 16px; background: #fff;
                   box-shadow: 0 1px 4px rgba(0,0,0,0.08); }}
  .summary-card .label {{ color: #666; font-size: 0.75rem; margin-bottom: 4px; }}
  .summary-card .value {{ font-size: 1.5rem; font-weight: 700; }}
  .chart-container {{ position: relative; width: 100%; }}
  .nav-link.active {{ font-weight: 600; }}
  .trend-up {{ color: #4CAF50; }}
  .trend-down {{ color: #D32F2F; }}
  .trend-flat {{ color: #FF9800; }}
  a.ticker-link {{ text-decoration: none; color: #1976D2; font-weight: 600; }}
  a.ticker-link:hover {{ text-decoration: underline; }}
  /* 状態バッジ */
  .status-badge {{ display: inline-flex; align-items: center; gap: 4px; padding: 3px 10px;
                   border-radius: 12px; font-size: 0.8rem; font-weight: 600; }}
  .status-safe {{ background: #E8F5E9; color: #2E7D32; }}
  .status-warn {{ background: #FFF3E0; color: #E65100; }}
  .status-danger {{ background: #FFEBEE; color: #C62828; }}
  /* ツールチップ */
  .tip {{ position: relative; cursor: help; border-bottom: 1px dotted #999; }}
  .tip:hover::after {{ content: attr(data-tip); position: absolute; left: 50%; transform: translateX(-50%);
      bottom: 110%; background: #333; color: #fff; padding: 6px 10px; border-radius: 6px;
      font-size: 0.75rem; white-space: nowrap; z-index: 10; font-weight: 400; }}
  /* 詳細列トグル */
  .detail-col {{ display: none; }}
  .show-detail .detail-col {{ display: table-cell; }}
  .toggle-btn {{ font-size: 0.8rem; cursor: pointer; color: #1976D2; border: 1px solid #1976D2;
                 background: #fff; padding: 3px 12px; border-radius: 16px; }}
  .toggle-btn:hover {{ background: #E3F2FD; }}
  .filter-group {{ display: inline-flex; gap: 4px; margin-right: 16px; }}
  .filter-btn {{ font-size: 0.8rem; cursor: pointer; padding: 3px 12px; border-radius: 16px;
                 border: 1px solid #ccc; background: #fff; color: #666; }}
  .filter-btn:hover {{ background: #f5f5f5; }}
  .filter-btn.active {{ background: #1976D2; color: #fff; border-color: #1976D2; }}
  /* AI アドバイザーカード */
  .llm-card {{ border-radius: 12px; padding: 16px; background: #fff;
               box-shadow: 0 1px 4px rgba(0,0,0,0.08); border-top: 3px solid #ccc; }}
  .llm-card .llm-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }}
  .llm-card .llm-provider {{ font-weight: 700; font-size: 0.95rem; }}
  .llm-card .llm-model {{ font-size: 0.75rem; color: #888; }}
  .llm-card .llm-body {{ font-size: 0.85rem; line-height: 1.6; white-space: pre-wrap;
                         word-break: break-word; }}
  .llm-card .llm-meta {{ font-size: 0.7rem; color: #aaa; margin-top: 8px; text-align: right; }}
</style>
</head>"""


def _navbar(active: str = "report") -> str:
    def _cls(name: str) -> str:
        return "nav-link active" if name == active else "nav-link"
    return f"""
<nav class="navbar navbar-expand-sm navbar-light bg-white mb-4 rounded shadow-sm px-3">
  <a class="navbar-brand fw-bold" href="/report">Investment Dashboard</a>
  <div class="navbar-nav ms-auto">
    <a class="{_cls('report')}" href="/report">Dashboard</a>
    <a class="{_cls('screening')}" href="/screening">Screening</a>
    <a class="{_cls('performance')}" href="/performance">Performance</a>
  </div>
</nav>"""


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
                   t.rsi14, t.atr14, t.ma5, t.ma25, t.ma75, t.ma200,
                   d.final_score, d.recommend_rank,
                   d.trend_score, d.pattern_a_score, d.pattern_b_score, d.liquidity_score,
                   u.sector
            FROM holdings h
            LEFT JOIN technical_snapshot t
              ON t.ticker = h.ticker
              AND t.date = (SELECT MAX(date) FROM technical_snapshot WHERE ticker = h.ticker)
            LEFT JOIN daily_signals d
              ON d.ticker = h.ticker
              AND d.date = (SELECT MAX(date) FROM daily_signals WHERE ticker = h.ticker)
            LEFT JOIN universe_master u ON u.ticker = h.ticker
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

    return _render_report(today_str, market_env, holdings, candidates, market_history, llm_feedback, near_misses)


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
            "SELECT ticker, entry_price, quantity, entry_date FROM holdings WHERE is_closed = 0",
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
                    total_pnl += (cp - ep) * qty
                    total_cost += ep * qty
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


@app.route("/api/sector-alloc")
def api_sector_alloc():
    """セクター配分データ"""
    today_str = date.today().strftime("%Y-%m-%d")

    with get_connection() as conn:
        rows = fetch_all(
            conn,
            """SELECT COALESCE(u.sector, '不明') AS sector,
                      SUM(h.entry_price * h.quantity) AS total_value
               FROM holdings h
               LEFT JOIN universe_master u ON u.ticker = h.ticker
               WHERE h.is_closed = 0
               GROUP BY sector
               ORDER BY total_value DESC""",
        )

    rows = [dict(r) for r in rows]
    return jsonify({
        "sectors": [r["sector"] for r in rows],
        "values": [round(r["total_value"] or 0) for r in rows],
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

def _render_report(today_str, market_env, holdings, candidates, market_history, llm_feedback=None, near_misses=None):
    market_env = dict(market_env) if market_env else {}
    holdings = [dict(h) for h in holdings]
    candidates = [dict(c) for c in candidates]
    market_history = [dict(m) for m in market_history]
    llm_feedback = [dict(f) for f in llm_feedback] if llm_feedback else []

    mode = market_env.get("mode", "unknown")
    mode_label = _MODE_LABEL.get(mode, mode)
    mode_color = _MODE_COLOR.get(mode, "#999")

    # --- サマリー計算 ---
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
        if h["market"] == "JP":
            jp_count += 1
        else:
            us_count += 1
        if cp <= 0:
            no_price_tickers.append(h["ticker"])
            continue
        total_pnl += (cp - ep) * qty
        total_cost += ep * qty
        total_market_val += cp * qty

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

    if mode in ("caution", "full_caution"):
        action_items.append(("info", f"🔴 {mode_label}中 — 新規買い候補は自動停止しています"))
    elif len(candidates) > 0:
        today_sigs = [c for c in candidates if c.get("signal_date") == today_str]
        if today_sigs:
            action_items.append(("info", f"📈 本日の検討候補: {len(today_sigs)}件（最終判断はご自身で行ってください）"))
        else:
            action_items.append(("ok", "✅ 本日新たな検討候補はありません"))
    else:
        action_items.append(("ok", "✅ 本日新たな検討候補はありません"))

    if not action_items:
        action_items.append(("ok", "✅ 特に対応が必要な事項はありません"))

    action_html = ""
    for level, msg in action_items:
        if level == "danger":
            bg = "#FFEBEE"
        elif level == "warn":
            bg = "#FFF3E0"
        elif level == "info":
            bg = "#E3F2FD"
        else:
            bg = "#E8F5E9"
        action_html += f'<div style="padding:8px 12px;border-radius:8px;background:{bg};margin-bottom:6px;font-size:0.9rem">{msg}</div>'

    # --- LLM アドバイザーセクション構築 ---
    _LLM_DISPLAY = {
        "openai": ("ChatGPT", "#10a37f", "💬"),
        "anthropic": ("Claude", "#d97706", "🧠"),
        "gemini": ("Gemini", "#4285F4", "✨"),
    }
    llm_cards_html = ""
    if llm_feedback:
        fb_date = llm_feedback[0].get("feedback_date", "")
        fb_cards = ""
        for fb in llm_feedback:
            prov = fb.get("provider", "")
            display_name, color, icon = _LLM_DISPLAY.get(prov, (prov, "#999", "💬"))
            model = fb.get("model", "")
            text = escape(fb.get("response_text", ""))
            batch = "夜バッチ" if fb.get("batch_type") == "evening" else "朝バッチ"
            created = fb.get("created_at", "")
            fb_cards += f"""
      <div class="col-md-4">
        <div class="llm-card" style="border-top-color:{color}">
          <div class="llm-header">
            <span style="font-size:1.2rem">{icon}</span>
            <span class="llm-provider" style="color:{color}">{display_name}</span>
            <span class="llm-model">{escape(model)}</span>
          </div>
          <div class="llm-body">{text}</div>
          <div class="llm-meta">{batch} · {created}</div>
        </div>
      </div>"""
        llm_cards_html = f"""
<div class="section">
  <h2>🤖 AIアドバイザー意見 <span class="text-muted fs-6">({fb_date})</span></h2>
  <div class="row g-3">{fb_cards}
  </div>
</div>"""

    # セクター配分用データ（holdings から）
    sector_map = {}
    for h in holdings:
        sec = h.get("sector") or "不明"
        val = float(h["entry_price"] or 0) * float(h["quantity"] or 1)
        sector_map[sec] = sector_map.get(sec, 0) + val
    sector_labels = json.dumps(list(sector_map.keys()), ensure_ascii=False)
    sector_values = json.dumps([round(v) for v in sector_map.values()])

    # --- 保有銘柄テーブル行 ---
    holding_rows = ""
    first_ticker_for_radar = ""
    for h in holdings:
        if not first_ticker_for_radar:
            first_ticker_for_radar = h["ticker"]

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

        target_str = "—"
        if tp and cp:
            dist = (tp - cp) / cp * 100
            target_str = f"{tp:,.2f}（{dist:+.1f}%）"

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
        rank_str = f"<span class='rank-badge' style='background:{rank_color}'>{escape(frank or '—')}</span>"

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

        # 状態バッジ（信号機方式: 損益 + トレンド + SL距離で総合判定）
        sl_dist_pct = ((cp - sl) / cp * 100) if cp and sl else 99
        if pnl_pct <= -5 or sl_dist_pct <= 2:
            status_cls = "status-danger"
            status_label = "危険"
        elif pnl_pct < 0 or (rsi is not None and rsi >= 70) or sl_dist_pct <= 5:
            status_cls = "status-warn"
            status_label = "注意"
        else:
            status_cls = "status-safe"
            status_label = "安心"

        holding_rows += f"""
        <tr>
          <td><a class="ticker-link" href="/stock/{escape(h['ticker'])}">{escape(h['ticker'])}</a></td>
          <td>{escape(h['name'] or '')}</td>
          <td>{f'{cp:,.2f}' if cp else '—'}</td>
          <td style="{pnl_color}">{pnl_str}</td>
          <td><span class="status-badge {status_cls}">{status_label}</span></td>
          <td>{sl:,.2f}</td>
          <td>{target_str}</td>
          <td>{days_str}</td>
          <td class="detail-col">{escape(h['market'] or '')}</td>
          <td class="detail-col">{f'{qty:.0f}' if qty else '—'}</td>
          <td class="detail-col" style="{rsi_color}">{rsi_str}</td>
          <td class="detail-col">{atr_str}</td>
          <td class="detail-col">{ma25_dev_str}</td>
          <td class="detail-col">{score_str}</td>
          <td class="detail-col">{rank_str}</td>
          <td class="detail-col">{trend_str}</td>
        </tr>"""

    # シグナル履歴テーブル行
    signal_rows = ""
    for s in candidates:
        sl_p = float(s["stop_loss_pct"] or 0)
        tg_p = sl_p * 2
        amt = s["recommended_amount"]
        currency = "円" if s["market"] == "JP" else "USD"
        amt_str = f"{amt:,.0f} {currency}" if amt else "—"
        signal_rows += f"""
        <tr>
          <td>{escape(s['signal_date'] or '')}</td>
          <td><a class="ticker-link" href="/stock/{escape(s['ticker'])}">{escape(s['ticker'])}</a></td>
          <td>{escape(s['name'] or '')}</td>
          <td>{escape(s['market'] or '')}</td>
          <td>{escape(_PATTERN_NAME.get(s['pattern'], s['pattern']))}</td>
          <td>{float(s['entry_price']):,.2f}</td>
          <td>{float(s['stop_loss_price']):,.2f}（-{sl_p:.1f}%）</td>
          <td>{float(s['target_price']):,.2f}（+{tg_p:.1f}%）</td>
          <td>{amt_str}</td>
        </tr>"""

    holding_empty = "<tr><td colspan='8' style='text-align:center;color:#999'>保有銘柄なし</td></tr>" if not holdings else ""
    signal_empty = "<tr><td colspan='9' style='text-align:center;color:#999'>直近7日間のシグナルなし</td></tr>" if not candidates else ""

    # 惜しかった銘柄カード生成
    near_misses = [dict(nm) for nm in near_misses] if near_misses else []
    _NM_TYPE_BADGE = {
        "filtered": ("🚫 見送り", "#D32F2F"),
        "demoted":  ("⚠️ 警戒モードで降格", "#FF5722"),
        "score_short": ("", "#FF9800"),
    }
    near_miss_cards = ""
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
            _nm_mkt = nm.get("market") or ""
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

        near_miss_cards += f"""
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

    # 市場環境タイムラインの JSON データ
    mh_dates = json.dumps([m["date"] for m in market_history])
    mh_n225 = json.dumps([m["n225_close"] for m in market_history])
    mh_n225_ma = json.dumps([m["n225_ma25"] for m in market_history])
    mh_sp500 = json.dumps([m["sp500_close"] for m in market_history])
    mh_sp500_ma = json.dumps([m["sp500_ma25"] for m in market_history])
    mh_modes = json.dumps([m["mode"] for m in market_history])

    return f"""{_common_head(f'Dashboard {today_str}')}
<body>
{_navbar('report')}

<h1 class="h4 mb-4">Dashboard <span class="text-muted fs-6">{today_str}</span></h1>

<!-- サマリーカード -->
<div class="row g-3 mb-4">
  <div class="col">
    <div class="summary-card">
      <div class="label">資産総額（時価）</div>
      <div class="value">{total_market_val:,.0f}</div>
      <div style="font-size:0.75rem;color:#666">投入額: {total_cost:,.0f}</div>
    </div>
  </div>
  <div class="col">
    <div class="summary-card">
      <div class="label"><span class="tip" data-tip="（現在値 − 購入価格）× 数量 の合計">損益合計（含み損益）</span></div>
      <div class="value {pnl_color_cls}">{total_pnl:+,.0f}</div>
      <div style="font-size:0.85rem" class="{pnl_color_cls}">{total_pnl_pct:+.1f}%</div>
      {no_price_warn}
    </div>
  </div>
  <div class="col">
    <div class="summary-card">
      <div class="label">市場環境 ({env_date})</div>
      <div class="mb-1"><span class="mode-badge" style="background:{mode_color}">{mode_label}</span></div>
      <div style="font-size:0.8rem">{jp_str}<br>{us_str}</div>
    </div>
  </div>
  <div class="col">
    <div class="summary-card">
      <div class="label">保有銘柄数</div>
      <div class="value">{len(holdings)}<span class="fs-6 ms-2">JP:{jp_count} / US:{us_count}</span></div>
    </div>
  </div>
  <div class="col">
    <div class="summary-card">
      <div class="label">直近7日シグナル</div>
      <div class="value">{len(candidates)}<span class="fs-6 ms-2">A:{sig_a} / B:{sig_b}</span></div>
    </div>
  </div>
</div>

<!-- 今日の行動 -->
<div class="summary-card mb-4">
  <h2>📋 今日の行動</h2>
  {action_html}
</div>

{llm_cards_html}

<!-- チャート行 -->
<div class="row g-3 mb-4">
  <div class="col-md-5">
    <div class="summary-card">
      <div class="label">損益推移（直近30営業日）</div>
      <div class="chart-container" style="height:220px">
        <canvas id="pnlChart"></canvas>
      </div>
    </div>
  </div>
  <div class="col-md-3">
    <div class="summary-card">
      <div class="label">スコア・レーダー</div>
      <div class="chart-container" style="height:220px">
        <canvas id="radarChart"></canvas>
      </div>
      <div class="mt-1">
        <select id="radarTicker" class="form-select form-select-sm" onchange="loadRadar()">
          {''.join(f'<option value="{escape(h["ticker"])}">{escape(h["ticker"])} {escape(h["name"] or "")}</option>' for h in holdings)}
        </select>
      </div>
    </div>
  </div>
  <div class="col-md-4">
    <div class="summary-card">
      <div class="label">セクター配分</div>
      <div class="chart-container" style="height:250px">
        <canvas id="sectorChart"></canvas>
      </div>
    </div>
  </div>
</div>

<!-- 保有銘柄テーブル -->
<div class="section">
  <div class="d-flex align-items-center gap-3 mb-2">
    <h2 class="mb-0">保有銘柄（{len(holdings)}件）</h2>
    <button class="toggle-btn" onclick="document.querySelector('#holdingsTable').classList.toggle('show-detail'); this.textContent = this.textContent === '詳細を表示' ? '詳細を隠す' : '詳細を表示'">詳細を表示</button>
  </div>
  <div class="table-responsive">
    <table id="holdingsTable" class="table table-bordered table-sm table-hover bg-white">
      <thead class="table-light">
        <tr>
          <th>Ticker</th><th>銘柄名</th><th>現在値</th>
          <th>損益</th><th>状態</th>
          <th><span class="tip" data-tip="損切りライン：ここまで下がったら売る価格">SL</span></th>
          <th>利確目安</th><th>保有日数</th>
          <th class="detail-col">市場</th><th class="detail-col">数量</th>
          <th class="detail-col"><span class="tip" data-tip="RSI：0〜100の数値。70以上は買われすぎ、30以下は売られすぎ">RSI</span></th>
          <th class="detail-col"><span class="tip" data-tip="ATR：1日あたりの値動きの大きさ（値が大きいほど変動が激しい）">ATR</span></th>
          <th class="detail-col"><span class="tip" data-tip="MA25乖離：25日平均線からどれだけ離れているか（%）">MA25乖離</span></th>
          <th class="detail-col">Score</th><th class="detail-col">Rank</th><th class="detail-col">Trend</th>
        </tr>
      </thead>
      <tbody>{holding_rows}{holding_empty}</tbody>
    </table>
  </div>
</div>

<!-- シグナル履歴 -->
<div class="section">
  <h2>直近7日間のシグナル履歴（{len(candidates)}件）</h2>
  <div class="table-responsive">
    <table class="table table-bordered table-sm table-hover bg-white">
      <thead class="table-light">
        <tr>
          <th>日付</th><th>Ticker</th><th>銘柄名</th><th>市場</th><th>パターン</th>
          <th>エントリー</th><th>損切りSL</th><th>利確目安</th><th>推奨投入額</th>
        </tr>
      </thead>
      <tbody>{signal_rows}{signal_empty}</tbody>
    </table>
  </div>
</div>

{"" if not near_misses else f"""
<!-- 惜しかった銘柄 -->
<div class="section">
  <h2>👀 惜しかった銘柄（{len(near_misses)}件）</h2>
  <p style="font-size:0.8rem;color:#666;margin-bottom:12px">
    通知対象にならなかった注目銘柄です。スコア不足・損切り条件・警戒モード降格などの理由が確認できます。
    <a href="/screening?filter=watch" style="margin-left:8px">📋 Screeningで全件を見る →</a>
  </p>
  {near_miss_cards}
</div>
"""}

<!-- 見送り条件リファレンス -->
<div class="section">
  <h2>📏 見送り条件リファレンス</h2>
  <p style="font-size:0.8rem;color:#666;margin-bottom:12px">
    パターン検出で候補（candidate）になっても、以下の条件に該当するとシグナル通知から除外されます。
  </p>
  <div class="row g-3">
    <div class="col-md-6">
      <div class="summary-card" style="border-left:4px solid #D32F2F">
        <div style="font-weight:700;margin-bottom:8px">🚫 損切り幅が狭すぎる</div>
        <table style="font-size:0.8rem;width:100%">
          <tr><td style="color:#666;width:40%">判定</td>
              <td>損切り幅(%) ＜ <strong>{_SKIP_SL_MIN_PCT}%</strong></td></tr>
          <tr><td style="color:#666">損切り幅の計算</td>
              <td>(終値 − SL) ÷ 終値 × 100</td></tr>
          <tr><td style="color:#666">SLの計算</td>
              <td>終値 − ATR14 × <strong>{_SKIP_ATR_MULT:.0f}</strong></td></tr>
          <tr><td style="color:#666">該当しやすい銘柄</td>
              <td>値動きが小さい（ATRが低い）銘柄</td></tr>
        </table>
      </div>
    </div>
    <div class="col-md-6">
      <div class="summary-card" style="border-left:4px solid #1976D2">
        <div style="font-weight:700;margin-bottom:8px">💰 推奨投入額の計算</div>
        <table style="font-size:0.8rem;width:100%">
          <tr><td style="color:#666;width:40%">計算式</td>
              <td>総資金 × リスク{_SKIP_RISK_PCT*100:.0f}% ÷ 損切り幅(%)</td></tr>
          <tr><td style="color:#666">投入額の上限</td>
              <td>min(総資金, 総資金÷最大保有数)</td></tr>
          <tr><td style="color:#666">現在の設定値</td>
              <td>JP: <strong>{_SKIP_CAPITAL_JPY:,.0f}円</strong> / US: <strong>${_SKIP_CAPITAL_USD:,.0f}</strong> / 最大<strong>{_SKIP_MAX_HOLDINGS}</strong>銘柄<br>
                  → JP 1銘柄上限 <strong>{_SKIP_CAPITAL_JPY/_SKIP_MAX_HOLDINGS:,.0f}円</strong> / US <strong>${_SKIP_CAPITAL_USD/_SKIP_MAX_HOLDINGS:,.0f}</strong></td></tr>
          <tr><td style="color:#666">取引単位</td>
              <td>端株対応（moomoo証券）— 1株未満でも購入可能</td></tr>
        </table>
      </div>
    </div>
  </div>
</div>

<!-- スクリーニングファネル -->
<div class="section">
  <h2>スクリーニングファネル <span class="text-muted fs-6" id="funnelDate"></span></h2>
  <p style="font-size:0.8rem;color:#666;margin-bottom:12px">
    全銘柄がどの段階で絞り込まれたかを示します。
    <span class="tip" data-tip="ユニバース→流動性フィルタ→シグナル検出→最終候補の順に絞り込まれます">詳細</span>
  </p>
  <div class="row g-3">
    <div class="col-md-6">
      <div class="summary-card">
        <div class="label">絞り込みファネル</div>
        <div class="chart-container" style="height:260px">
          <canvas id="funnelChart"></canvas>
        </div>
      </div>
    </div>
    <div class="col-md-6">
      <div class="row g-3">
        <div class="col-6">
          <div class="summary-card">
            <div class="label">パターンA 条件充足率</div>
            <div id="condATable" style="font-size:0.8rem">読み込み中...</div>
          </div>
        </div>
        <div class="col-6">
          <div class="summary-card">
            <div class="label">パターンB 条件充足率</div>
            <div id="condBTable" style="font-size:0.8rem">読み込み中...</div>
          </div>
        </div>
        <div class="col-12">
          <div class="summary-card">
            <div class="label">スコア分布</div>
            <div class="chart-container" style="height:120px">
              <canvas id="scoreDistChart"></canvas>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- 市場環境タイムライン -->
<div class="section">
  <h2>市場環境タイムライン</h2>
  <div class="row g-3">
    <div class="col-md-6">
      <div class="summary-card">
        <div class="label">日経平均 推移</div>
        <div class="chart-container" style="height:220px">
          <canvas id="n225Chart"></canvas>
        </div>
      </div>
    </div>
    <div class="col-md-6">
      <div class="summary-card">
        <div class="label">S&amp;P500 推移</div>
        <div class="chart-container" style="height:220px">
          <canvas id="sp500Chart"></canvas>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
// --- 損益推移チャート ---
fetch('/api/portfolio-pnl').then(r=>r.json()).then(data=>{{
  new Chart(document.getElementById('pnlChart'),{{
    type:'line',
    data:{{
      labels: data.dates,
      datasets:[{{
        label:'損益 (%)',
        data: data.pnl_pct,
        borderColor:'#1976D2',
        backgroundColor:'rgba(25,118,210,0.1)',
        fill:true,
        tension:0.3,
        pointRadius:1
      }}]
    }},
    options:{{
      responsive:true, maintainAspectRatio:false,
      plugins:{{legend:{{display:false}}}},
      scales:{{x:{{display:true,ticks:{{maxTicksLimit:6,font:{{size:10}}}}}},
               y:{{ticks:{{callback:v=>v+'%'}}}}}}
    }}
  }});
}});

// --- レーダーチャート ---
let radarChartInstance = null;
function loadRadar() {{
  const ticker = document.getElementById('radarTicker')?.value || '';
  if(!ticker) return;
  fetch('/api/score-radar?ticker='+ticker).then(r=>r.json()).then(data=>{{
    if(radarChartInstance) radarChartInstance.destroy();
    radarChartInstance = new Chart(document.getElementById('radarChart'),{{
      type:'radar',
      data:{{
        labels:[['トレンド'],['パターンA','(ブレイクアウト)'],['パターンB','(押し目買い)'],['流動性']],
        datasets:[{{
          data:[data.trend, data.pattern_a, data.pattern_b, data.liquidity],
          backgroundColor:'rgba(25,118,210,0.2)',
          borderColor:'#1976D2',
          pointBackgroundColor:'#1976D2'
        }}]
      }},
      options:{{
        responsive:true, maintainAspectRatio:false,
        plugins:{{legend:{{display:false}}}},
        scales:{{r:{{min:0, max:100, ticks:{{stepSize:20,font:{{size:9}}}},
          pointLabels:{{font:{{size:9}}}}
        }}}}
      }}
    }});
  }});
}}
loadRadar();

// --- セクターチャート ---
(function(){{
  const labels = {sector_labels};
  const values = {sector_values};
  if(labels.length > 0) {{
    new Chart(document.getElementById('sectorChart'),{{
      type:'doughnut',
      data:{{
        labels: labels,
        datasets:[{{data: values,
          backgroundColor:['#1976D2','#4CAF50','#FF9800','#D32F2F','#9C27B0','#00BCD4','#795548','#607D8B','#E91E63','#3F51B5']
        }}]
      }},
      options:{{responsive:true, maintainAspectRatio:false,
        plugins:{{legend:{{position:'right',labels:{{font:{{size:10}}}}}}}}
      }}
    }});
  }}
}})();

// --- 市場環境チャート ---
(function(){{
  const dates = {mh_dates};
  const modeColorMap = {{'normal':'rgba(76,175,80,0.15)','caution':'rgba(255,152,0,0.15)','full_caution':'rgba(211,47,47,0.15)'}};
  const modes = {mh_modes};

  function makeIndexChart(canvasId, data, maData, label, color) {{
    new Chart(document.getElementById(canvasId),{{
      type:'line',
      data:{{
        labels: dates,
        datasets:[
          {{label:label, data:data, borderColor:color, borderWidth:2, pointRadius:0, tension:0.2}},
          {{label:'MA25', data:maData, borderColor:'#FF9800', borderWidth:1, borderDash:[4,4], pointRadius:0, tension:0.2}}
        ]
      }},
      options:{{
        responsive:true, maintainAspectRatio:false,
        plugins:{{legend:{{labels:{{font:{{size:10}}}}}}}},
        scales:{{x:{{ticks:{{maxTicksLimit:6,font:{{size:10}}}}}},y:{{ticks:{{font:{{size:10}}}}}}}}
      }}
    }});
  }}
  makeIndexChart('n225Chart', {mh_n225}, {mh_n225_ma}, '日経平均', '#D32F2F');
  makeIndexChart('sp500Chart', {mh_sp500}, {mh_sp500_ma}, 'S&P500', '#1976D2');
}})();

// --- スクリーニングファネル ---
fetch('/api/screening-funnel').then(r=>r.json()).then(data=>{{
  if(data.error) return;
  const f = data.funnel;
  document.getElementById('funnelDate').textContent = '(' + data.date + ')';

  // ファネル横棒チャート
  new Chart(document.getElementById('funnelChart'),{{
    type:'bar',
    data:{{
      labels:['ユニバース','流動性フィルタ通過','candidate','watch','最終通知'],
      datasets:[{{
        data:[f.universe, f.screened, f.candidate, f.watch, f.notified],
        backgroundColor:['#90CAF9','#42A5F5','#4CAF50','#FF9800','#D32F2F'],
        borderRadius:4
      }}]
    }},
    options:{{
      indexAxis:'y', responsive:true, maintainAspectRatio:false,
      plugins:{{
        legend:{{display:false}},
        tooltip:{{callbacks:{{
          label:function(ctx){{
            const v=ctx.raw;
            const pct=f.universe>0?(v/f.universe*100).toFixed(1):'0';
            return v.toLocaleString()+' 件 ('+pct+'%)';
          }}
        }}}}
      }},
      scales:{{
        x:{{type:'logarithmic',ticks:{{font:{{size:10}}}}}},
        y:{{ticks:{{font:{{size:11}}}}}}
      }}
    }}
  }});

  // パターンA 条件充足率テーブル
  const pa = data.pattern_a;
  const aLabels = [
    ['MA200上昇','ma200_rising'],['MA整列','ma_aligned'],['52週高値近傍','high_52w_near'],
    ['出来高急増','volume_surge'],['RSI適正','rsi_ok'],['MA25上方','above_ma25']
  ];
  let aHtml='<table class="table table-sm table-bordered mb-0" style="font-size:0.8rem"><tbody>';
  aLabels.forEach(function(pair){{
    const cnt=pa[pair[1]]; const pct=pa.total>0?(cnt/pa.total*100).toFixed(1):'0';
    const w=pa.total>0?Math.round(cnt/pa.total*100):0;
    aHtml+='<tr><td style="width:45%">'+pair[0]+'</td>'
      +'<td><div style="background:#E3F2FD;border-radius:4px;overflow:hidden">'
      +'<div style="width:'+w+'%;background:#42A5F5;height:16px;border-radius:4px"></div></div></td>'
      +'<td style="width:25%;text-align:right">'+cnt+' <span style="color:#999">('+pct+'%)</span></td></tr>';
  }});
  aHtml+='</tbody></table><div style="color:#999;font-size:0.7rem;margin-top:4px">対象: '+pa.total.toLocaleString()+' 銘柄</div>';
  document.getElementById('condATable').innerHTML=aHtml;

  // パターンB 条件充足率テーブル
  const pb = data.pattern_b;
  const bLabels = [
    ['MA200上昇','ma200_rising'],['MA整列','ma_aligned'],['押し目タッチ','pullback_touched'],
    ['反発確認','bounced'],['MA25上昇','ma25_rising'],['RSI適正','rsi_ok']
  ];
  let bHtml='<table class="table table-sm table-bordered mb-0" style="font-size:0.8rem"><tbody>';
  bLabels.forEach(function(pair){{
    const cnt=pb[pair[1]]; const pct=pb.total>0?(cnt/pb.total*100).toFixed(1):'0';
    const w=pb.total>0?Math.round(cnt/pb.total*100):0;
    bHtml+='<tr><td style="width:45%">'+pair[0]+'</td>'
      +'<td><div style="background:#FFF3E0;border-radius:4px;overflow:hidden">'
      +'<div style="width:'+w+'%;background:#FF9800;height:16px;border-radius:4px"></div></div></td>'
      +'<td style="width:25%;text-align:right">'+cnt+' <span style="color:#999">('+pct+'%)</span></td></tr>';
  }});
  bHtml+='</tbody></table><div style="color:#999;font-size:0.7rem;margin-top:4px">対象: '+pb.total.toLocaleString()+' 銘柄</div>';
  document.getElementById('condBTable').innerHTML=bHtml;

  // スコア分布チャート
  const sd = data.score_distribution;
  const buckets = ['0-19','20-39','40-59','60-79','80-100'];
  const bucketColors = ['#BDBDBD','#90CAF9','#42A5F5','#FF9800','#4CAF50'];
  new Chart(document.getElementById('scoreDistChart'),{{
    type:'bar',
    data:{{
      labels: buckets,
      datasets:[{{
        data: buckets.map(function(b){{ return sd[b]||0; }}),
        backgroundColor: bucketColors,
        borderRadius:4
      }}]
    }},
    options:{{
      responsive:true, maintainAspectRatio:false,
      plugins:{{legend:{{display:false}}}},
      scales:{{
        x:{{ticks:{{font:{{size:10}}}}}},
        y:{{ticks:{{font:{{size:10}},stepSize:1}}}}
      }}
    }}
  }});
}});
</script>

</body>
</html>"""


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
    fund_rows = ""
    if has_fundamentals:
        def _fv(key, fmt=".2f"):
            v = fundamentals.get(key)
            return f"{v:{fmt}}" if v is not None else "—"
        fund_rows = f"""
        <tr><td>PER</td><td>{_fv('per')}</td></tr>
        <tr><td>PBR</td><td>{_fv('pbr')}</td></tr>
        <tr><td>配当利回り</td><td>{_fv('dividend_yield', '.2f')}%</td></tr>
        <tr><td>EPS</td><td>{_fv('eps')}</td></tr>
        <tr><td>BPS</td><td>{_fv('bps')}</td></tr>
        """

    # リスク指標
    var_val = risk_data.get("var_99_1d")
    beta_val = risk_data.get("beta_60d")
    sharpe_val = risk_data.get("sharpe_60d")
    var_str = f"{var_val * 100:.2f}%" if var_val is not None else "計算不可"
    beta_str = f"{beta_val:.2f}" if beta_val is not None else "計算不可"
    sharpe_str = f"{sharpe_val:.2f}" if sharpe_val is not None else "計算不可"

    # シグナル履歴テーブル
    sig_rows = ""
    for s in signals:
        s = dict(s)
        sl_p = float(s.get("stop_loss_pct") or 0)
        sig_rows += f"""
        <tr>
          <td>{escape(s.get('signal_date') or '')}</td>
          <td>{escape(_PATTERN_NAME.get(s.get('pattern',''), s.get('pattern','')))}</td>
          <td>{float(s.get('entry_price') or 0):,.2f}</td>
          <td>{float(s.get('stop_loss_price') or 0):,.2f}（-{sl_p:.1f}%）</td>
          <td>{float(s.get('target_price') or 0):,.2f}</td>
          <td>{escape(s.get('market_mode') or '')}</td>
        </tr>"""
    sig_empty = "<tr><td colspan='6' style='text-align:center;color:#999'>シグナルなし</td></tr>" if not signals else ""

    return f"""{_common_head(f'{ticker} - {name}')}
<body>
{_navbar('')}

<div class="mb-3">
  <a href="/report" class="text-decoration-none">&larr; Dashboard に戻る</a>
</div>

<!-- 銘柄ヘッダ -->
<div class="row align-items-end mb-4">
  <div class="col">
    <h1 class="h4 mb-0">{escape(ticker)} <span class="text-muted">{escape(name)}</span></h1>
    <div class="text-muted" style="font-size:0.85rem">
      {escape(market)} | セクター: {escape(sector)} | 業種: {escape(industry)}
    </div>
  </div>
  <div class="col-auto text-end">
    <div style="font-size:1.8rem; font-weight:700">{cp:,.2f}</div>
    <div class="{change_color}" style="font-size:1rem">{change_pct:+.2f}%</div>
    <div class="text-muted" style="font-size:0.75rem">52W: {l52_str} — {h52_str}</div>
  </div>
</div>

<div class="row g-3 mb-4">
  <!-- 価格チャート -->
  <div class="col-md-8">
    <div class="summary-card">
      <div class="label">価格推移（60営業日）</div>
      <div class="chart-container" style="height:300px">
        <canvas id="priceChart"></canvas>
      </div>
    </div>
  </div>
  <!-- スコアレーダー -->
  <div class="col-md-4">
    <div class="summary-card">
      <div class="label">スコア: {fscore if fscore is not None else '—'}
        <span class="rank-badge ms-2" style="background:{rank_color}">{escape(frank)}</span>
      </div>
      <div class="chart-container" style="height:200px">
        <canvas id="stockRadar"></canvas>
      </div>
      <div class="mt-2" style="font-size:0.8rem; color:#666">{escape(reason)}</div>
    </div>
  </div>
</div>

<!-- テクニカル + ファンダメンタルズ + リスク -->
<div class="row g-3 mb-4">
  <div class="col-md-4">
    <div class="summary-card">
      <h2>テクニカル指標</h2>
      <table class="table table-sm mb-0">
        <tr><td><span class="tip" data-tip="買われすぎ・売られすぎの指標。70以上は過熱、30以下は売られすぎ">RSI (14)</span></td><td>
          <div style="display:flex;align-items:center;gap:8px">
            <span>{rsi_str}</span>
            <div style="flex:1;height:8px;background:#eee;border-radius:4px">
              <div style="width:{rsi_width}%;height:100%;background:{rsi_bar_color};border-radius:4px"></div>
            </div>
          </div>
        </td></tr>
        <tr><td><span class="tip" data-tip="1日あたりの平均的な値動き幅。大きいほどボラティリティが高い">ATR (14)</span></td><td>{_tv('atr14')}</td></tr>
        <tr><td>MA5</td><td>{_tv('ma5')}</td></tr>
        <tr><td>MA25</td><td>{_tv('ma25')}</td></tr>
        <tr><td>MA75</td><td>{_tv('ma75')}</td></tr>
        <tr><td>MA200</td><td>{_tv('ma200')}</td></tr>
        <tr><td>出来高平均(20d)</td><td>{vol_str}</td></tr>
      </table>
    </div>
  </div>
  <div class="col-md-4">
    <div class="summary-card">
      <h2>ファンダメンタルズ{' (JP)' if has_fundamentals else ''}</h2>
      {'<table class="table table-sm mb-0">' + fund_rows + '</table>' if has_fundamentals else '<div class="text-muted mt-3">データなし</div>'}
    </div>
  </div>
  <div class="col-md-4">
    <div class="summary-card">
      <h2>リスク指標 (60日)</h2>
      <table class="table table-sm mb-0">
        <tr>
          <td><span class="tip" data-tip="最悪ケースで1日にどれだけ下がりうるか（99%の確率でこの範囲に収まる）">VaR (99%, 1日)</span></td>
          <td>{var_str}</td>
        </tr>
        <tr>
          <td><span class="tip" data-tip="市場全体と比べた値動きの大きさ。1.0なら市場と同じ、1.5なら1.5倍動く">ベータ値</span></td>
          <td>{beta_str}</td>
        </tr>
        <tr>
          <td><span class="tip" data-tip="リスクに見合ったリターンが得られているか。高いほど効率よく利益が出ている">シャープレシオ</span></td>
          <td>{sharpe_str}</td>
        </tr>
      </table>
      <div style="font-size:0.7rem;color:#888;margin-top:8px">
        ※ 過去60日の値動きから計算した参考値です
      </div>
    </div>
  </div>
</div>

<!-- シグナル履歴 -->
<div class="section">
  <h2>シグナル履歴</h2>
  <div class="table-responsive">
    <table class="table table-bordered table-sm table-hover bg-white">
      <thead class="table-light">
        <tr><th>日付</th><th>パターン</th><th>エントリー</th><th>損切りSL</th><th>利確目安</th><th>市場モード</th></tr>
      </thead>
      <tbody>{sig_rows}{sig_empty}</tbody>
    </table>
  </div>
</div>

<script>
// 価格チャート
fetch('/api/price-history?ticker={ticker}&days=60').then(r=>r.json()).then(data=>{{
  new Chart(document.getElementById('priceChart'),{{
    type:'line',
    data:{{
      labels: data.dates,
      datasets:[
        {{label:'終値', data:data.close, borderColor:'#1976D2', borderWidth:2, pointRadius:0, tension:0.2}},
        {{label:'MA25', data:data.ma25, borderColor:'#FF9800', borderWidth:1, borderDash:[4,4], pointRadius:0, tension:0.2}},
        {{label:'MA75', data:data.ma75, borderColor:'#9E9E9E', borderWidth:1, borderDash:[2,2], pointRadius:0, tension:0.2}}
      ]
    }},
    options:{{
      responsive:true, maintainAspectRatio:false,
      plugins:{{legend:{{labels:{{font:{{size:10}}}}}}}},
      scales:{{x:{{ticks:{{maxTicksLimit:8,font:{{size:10}}}}}},y:{{ticks:{{font:{{size:10}}}}}}}}
    }}
  }});
}});

// レーダーチャート
fetch('/api/score-radar?ticker={ticker}').then(r=>r.json()).then(data=>{{
  new Chart(document.getElementById('stockRadar'),{{
    type:'radar',
    data:{{
      labels:[['トレンド'],['パターンA','(ブレイクアウト)'],['パターンB','(押し目買い)'],['流動性']],
      datasets:[{{
        data:[data.trend, data.pattern_a, data.pattern_b, data.liquidity],
        backgroundColor:'rgba(25,118,210,0.2)',
        borderColor:'#1976D2',
        pointBackgroundColor:'#1976D2'
      }}]
    }},
    options:{{
      responsive:true, maintainAspectRatio:false,
      plugins:{{legend:{{display:false}}}},
      scales:{{r:{{min:0, max:100, ticks:{{stepSize:20,font:{{size:9}}}},
        pointLabels:{{font:{{size:9}}}}
      }}}}
    }}
  }});
}});
</script>

</body>
</html>"""


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

    # パターン別テーブル
    pat_rows = ""
    for p in pattern_stats:
        pat_name = _PATTERN_NAME.get(p.get("pattern", ""), p.get("pattern", ""))
        pt = p.get("total") or 0
        pw = p.get("wins") or 0
        pa = p.get("avg_pnl") or 0
        pwr = (pw / pt * 100) if pt > 0 else 0
        pat_rows += f"""
        <tr>
          <td>{escape(pat_name)}</td>
          <td>{pt}</td>
          <td>{pw}</td>
          <td>{pwr:.1f}%</td>
          <td style="color:{'#4CAF50' if pa >= 0 else '#D32F2F'}">{pa:+.2f}%</td>
        </tr>"""

    # トレード一覧テーブル
    trade_rows = ""
    for t in reversed(all_trades[-50:]):  # 直近50件
        pnl = float(t.get("pnl_pct") or 0)
        pnl_color = "color:#4CAF50" if pnl >= 0 else "color:#D32F2F"
        reason_label = {
            "stop_loss": "損切り", "take_profit": "利確",
            "manual": "手動", "forced_stop": "強制停止",
        }.get(t.get("exit_reason", ""), t.get("exit_reason") or "—")
        pat_name = _PATTERN_NAME.get(t.get("pattern", ""), t.get("pattern", ""))
        trade_rows += f"""
        <tr>
          <td>{escape(t.get('actual_exit_date') or '')}</td>
          <td><a class="ticker-link" href="/stock/{escape(t['ticker'])}">{escape(t['ticker'])}</a></td>
          <td>{escape(pat_name)}</td>
          <td style="{pnl_color}">{pnl:+.2f}%</td>
          <td>{escape(reason_label)}</td>
        </tr>"""

    return f"""{_common_head('Performance Analysis')}
<body>
{_navbar('performance')}

<h1 class="h4 mb-4">Performance Analysis</h1>

<!-- サマリーカード -->
<div class="row g-3 mb-4">
  <div class="col-md-2">
    <div class="summary-card">
      <div class="label">総トレード数</div>
      <div class="value">{total}</div>
    </div>
  </div>
  <div class="col-md-2">
    <div class="summary-card">
      <div class="label">勝率</div>
      <div class="value {'trend-up' if win_rate >= 50 else 'trend-down'}">{win_rate:.1f}%</div>
      <div style="font-size:0.75rem;color:#666">{wins}W / {losses}L</div>
    </div>
  </div>
  <div class="col-md-2">
    <div class="summary-card">
      <div class="label">平均損益</div>
      <div class="value {'trend-up' if avg_pnl >= 0 else 'trend-down'}">{avg_pnl:+.2f}%</div>
    </div>
  </div>
  <div class="col-md-2">
    <div class="summary-card">
      <div class="label"><span class="tip" data-tip="プロフィットファクター：利益÷損失の比率。1.0以上なら利益が出ている">PF（利益率）</span></div>
      <div class="value">{pf:.2f}</div>
      <div style="font-size:0.7rem;color:#888">{('利益 &gt; 損失 👍' if pf >= 1.0 else '損失 &gt; 利益')}</div>
    </div>
  </div>
  <div class="col-md-2">
    <div class="summary-card">
      <div class="label">Best</div>
      <div class="value trend-up">{best:+.2f}%</div>
    </div>
  </div>
  <div class="col-md-2">
    <div class="summary-card">
      <div class="label">Worst</div>
      <div class="value trend-down">{worst:+.2f}%</div>
    </div>
  </div>
</div>

<!-- チャート -->
<div class="row g-3 mb-4">
  <div class="col-md-6">
    <div class="summary-card">
      <div class="label">累積損益曲線</div>
      <div class="chart-container" style="height:250px">
        <canvas id="cumChart"></canvas>
      </div>
    </div>
  </div>
  <div class="col-md-6">
    <div class="summary-card">
      <div class="label">月別損益</div>
      <div class="chart-container" style="height:250px">
        <canvas id="monthlyChart"></canvas>
      </div>
    </div>
  </div>
</div>

<!-- パターン別成績 -->
<div class="section">
  <h2>パターン別成績</h2>
  <div class="table-responsive">
    <table class="table table-bordered table-sm bg-white" style="max-width:600px">
      <thead class="table-light">
        <tr><th>パターン</th><th>トレード数</th><th>勝ち</th><th>勝率</th><th>平均損益</th></tr>
      </thead>
      <tbody>{pat_rows if pat_rows else "<tr><td colspan='5' style='text-align:center;color:#999'>データなし</td></tr>"}</tbody>
    </table>
  </div>
</div>

<!-- トレード一覧 -->
<div class="section">
  <h2>直近トレード（最新50件）</h2>
  <div class="table-responsive">
    <table class="table table-bordered table-sm table-hover bg-white">
      <thead class="table-light">
        <tr><th>決済日</th><th>Ticker</th><th>パターン</th><th>損益</th><th>理由</th></tr>
      </thead>
      <tbody>{trade_rows if trade_rows else "<tr><td colspan='5' style='text-align:center;color:#999'>データなし</td></tr>"}</tbody>
    </table>
  </div>
</div>

<script>
// 累積損益
fetch('/api/perf-cumulative').then(r=>r.json()).then(data=>{{
  new Chart(document.getElementById('cumChart'),{{
    type:'line',
    data:{{
      labels: data.dates,
      datasets:[{{
        label:'累積損益 (%)',
        data: data.cumulative_pnl,
        borderColor:'#1976D2',
        backgroundColor:'rgba(25,118,210,0.1)',
        fill:true, tension:0.3, pointRadius:2
      }}]
    }},
    options:{{
      responsive:true, maintainAspectRatio:false,
      plugins:{{legend:{{display:false}}}},
      scales:{{x:{{ticks:{{maxTicksLimit:8,font:{{size:10}}}}}},
               y:{{ticks:{{callback:v=>v+'%'}}}}}}
    }}
  }});
}});

// 月別損益
fetch('/api/perf-monthly').then(r=>r.json()).then(data=>{{
  new Chart(document.getElementById('monthlyChart'),{{
    type:'bar',
    data:{{
      labels: data.months,
      datasets:[{{
        label:'月別損益 (%)',
        data: data.pnl,
        backgroundColor: data.pnl.map(v => v >= 0 ? 'rgba(76,175,80,0.7)' : 'rgba(211,47,47,0.7)')
      }}]
    }},
    options:{{
      responsive:true, maintainAspectRatio:false,
      plugins:{{legend:{{display:false}}}},
      scales:{{x:{{ticks:{{font:{{size:10}}}}}},
               y:{{ticks:{{callback:v=>v+'%'}}}}}}
    }}
  }});
}});
</script>

</body>
</html>"""


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

    # --- テーブル行 ---
    table_rows = ""
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

        table_rows += f"""
        <tr data-label="{escape(label)}" data-market="{escape(market)}"
            data-pattern="{escape(pattern)}" data-score="{fscore or 0}">
          <td><a class="ticker-link" href="/stock/{escape(ticker)}">{escape(ticker)}</a></td>
          <td>{escape(name)}</td>
          <td>{escape(market)}</td>
          <td>{pat_html}</td>
          <td><span class="rank-badge" style="background:{lbl_color}">{lbl_text}</span></td>
          <td style="white-space:nowrap">{conds_html}</td>
          <td>{score_str}</td>
          <td><span class="rank-badge" style="background:{rank_color}">{escape(frank)}</span></td>
          <td>{verdict_detail}</td>
          <td>{reason_html}</td>
          <td class="detail-col">{close_str}</td>
          <td class="detail-col" style="{rsi_color}">{rsi_str}</td>
          <td class="detail-col">{vr_str}</td>
          <td class="detail-col">{escape(sector)}</td>
        </tr>"""

    return f"""{_common_head(f'Screening {target_date}')}
<body>
{_navbar('screening')}

<h1 class="h4 mb-4">Screening List <span class="text-muted fs-6">{target_date}</span></h1>

<!-- サマリーカード -->
<div class="row g-3 mb-4">
  <div class="col">
    <div class="summary-card">
      <div class="label">スクリーニング通過</div>
      <div class="value">{total}</div>
    </div>
  </div>
  <div class="col">
    <div class="summary-card">
      <div class="label">候補 (candidate)</div>
      <div class="value" style="color:#4CAF50">{cand_cnt}</div>
    </div>
  </div>
  <div class="col">
    <div class="summary-card">
      <div class="label">監視 (watch)</div>
      <div class="value" style="color:#FF9800">{watch_cnt}</div>
    </div>
  </div>
  <div class="col">
    <div class="summary-card">
      <div class="label">スキップ (skip)</div>
      <div class="value" style="color:#9E9E9E">{skip_cnt}</div>
    </div>
  </div>
</div>

<!-- フィルタ -->
<div class="summary-card mb-4">
  <div class="d-flex flex-wrap align-items-center gap-2">
    <span style="font-size:0.8rem;font-weight:600;color:#666">ラベル:</span>
    <div class="filter-group">
      <button class="filter-btn active" onclick="setFilter('label','candidate',this)">候補</button>
      <button class="filter-btn" onclick="setFilter('label','watch',this)">監視</button>
      <button class="filter-btn" onclick="setFilter('label','skip',this)">スキップ</button>
      <button class="filter-btn" onclick="setFilter('label','all',this)">すべて</button>
    </div>
    <span style="font-size:0.8rem;font-weight:600;color:#666">市場:</span>
    <div class="filter-group">
      <button class="filter-btn active" onclick="setFilter('market','all',this)">All</button>
      <button class="filter-btn" onclick="setFilter('market','JP',this)">JP</button>
      <button class="filter-btn" onclick="setFilter('market','US',this)">US</button>
    </div>
    <span style="font-size:0.8rem;font-weight:600;color:#666">パターン:</span>
    <div class="filter-group">
      <button class="filter-btn active" onclick="setFilter('pattern','all',this)">All</button>
      <button class="filter-btn" onclick="setFilter('pattern','A',this)">A</button>
      <button class="filter-btn" onclick="setFilter('pattern','B',this)">B</button>
    </div>
    <span id="filterCount" style="font-size:0.8rem;color:#999;margin-left:auto"></span>
    <button class="toggle-btn" onclick="document.querySelector('#screeningTable').classList.toggle('show-detail');this.textContent=this.textContent==='詳細を表示'?'詳細を隠す':'詳細を表示'">詳細を表示</button>
  </div>
</div>

<!-- テーブル -->
<div class="section">
  <div class="table-responsive">
    <table id="screeningTable" class="table table-bordered table-sm table-hover bg-white">
      <thead class="table-light">
        <tr>
          <th>Ticker</th><th>銘柄名</th><th>市場</th>
          <th><span class="tip" data-tip="A=モメンタムブレイクアウト B=押し目買い">PT</span></th>
          <th>ラベル</th>
          <th><span class="tip" data-tip="緑=条件充足 灰=未充足。ホバーで条件名を表示">条件 (6項目)</span></th>
          <th id="sortScore" style="cursor:pointer"><span class="tip" data-tip="クリックで並び替え">Score ⇅</span></th>
          <th>Rank</th>
          <th><span class="tip" data-tip="買い候補/監視/対象外の判定と未達条件">買い判定</span></th>
          <th><span class="tip" data-tip="スコアリングの理由（主要な加点・減点要因）">理由</span></th>
          <th class="detail-col">終値</th>
          <th class="detail-col"><span class="tip" data-tip="70以上=買われすぎ 30以下=売られすぎ">RSI</span></th>
          <th class="detail-col"><span class="tip" data-tip="出来高 ÷ 20日平均出来高">出来高比</span></th>
          <th class="detail-col">セクター</th>
        </tr>
      </thead>
      <tbody>{table_rows}</tbody>
    </table>
  </div>
</div>

<script>
// URL パラメータから初期フィルタを取得（例: /screening?filter=watch）
const _urlFilter = new URLSearchParams(window.location.search).get('filter');
const _validLabels = ['candidate', 'watch', 'skip', 'all'];
let filterLabel = (_urlFilter && _validLabels.includes(_urlFilter)) ? _urlFilter : 'candidate';
let filterMarket = 'all';
let filterPattern = 'all';
let sortAsc = false;

// URL パラメータで初期フィルタが指定された場合、ボタンのアクティブ状態を同期
if (_urlFilter && _validLabels.includes(_urlFilter)) {{
  document.querySelectorAll('.filter-group')[0].querySelectorAll('.filter-btn').forEach(function(b) {{
    b.classList.remove('active');
    if ((_urlFilter === 'candidate' && b.textContent === '候補') ||
        (_urlFilter === 'watch' && b.textContent === '監視') ||
        (_urlFilter === 'skip' && b.textContent === 'スキップ') ||
        (_urlFilter === 'all' && b.textContent === 'すべて')) {{
      b.classList.add('active');
    }}
  }});
}}

function setFilter(type, value, btn) {{
  if (type === 'label') filterLabel = value;
  else if (type === 'market') filterMarket = value;
  else if (type === 'pattern') filterPattern = value;
  // ボタンのアクティブ状態を切り替え
  btn.parentElement.querySelectorAll('.filter-btn').forEach(function(b){{ b.classList.remove('active'); }});
  btn.classList.add('active');
  applyFilters();
}}

function applyFilters() {{
  const rows = document.querySelectorAll('#screeningTable tbody tr');
  let shown = 0;
  rows.forEach(function(tr) {{
    const matchLabel = (filterLabel === 'all' || tr.dataset.label === filterLabel);
    const matchMarket = (filterMarket === 'all' || tr.dataset.market === filterMarket);
    const matchPattern = (filterPattern === 'all' || tr.dataset.pattern === filterPattern);
    if (matchLabel && matchMarket && matchPattern) {{
      tr.style.display = '';
      shown++;
    }} else {{
      tr.style.display = 'none';
    }}
  }});
  document.getElementById('filterCount').textContent = shown + ' / ' + rows.length + ' 件表示中';
}}

// Score ソート
document.getElementById('sortScore').addEventListener('click', function() {{
  const tbody = document.querySelector('#screeningTable tbody');
  const rows = Array.from(tbody.querySelectorAll('tr'));
  sortAsc = !sortAsc;
  rows.sort(function(a, b) {{
    const va = parseFloat(a.dataset.score) || 0;
    const vb = parseFloat(b.dataset.score) || 0;
    return sortAsc ? va - vb : vb - va;
  }});
  rows.forEach(function(r) {{ tbody.appendChild(r); }});
  applyFilters();
}});

// 初期表示
applyFilters();
</script>

</body>
</html>"""


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
