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
  /api/benchmark-comparison - 総資産 vs S&P500 ベンチマーク比較 JSON
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

from flask import Flask, render_template, request, jsonify, redirect, url_for
from markupsafe import escape

from src.config import CONFIG
from src.data_store import fetch_all, fetch_one, get_connection, insert_one
from src.logger import get_logger
from scripts.routes.api_routes import register_api_routes

logger = get_logger(__name__)

_SERVER_CFG = CONFIG.get("report_server", {})
_HOST = _SERVER_CFG.get("host", "127.0.0.1")
_PORT = int(_SERVER_CFG.get("port", 5100))

_CAP_CFG = CONFIG.get("capital", {})
_MAX_HOLDINGS = int(_CAP_CFG.get("max_concurrent_holdings", 3))

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


def _close_price_jpy(close: float | None, market: str, usdjpy: float) -> float | None:
    """終値を1株あたりの円建て価格に変換する。JP はそのまま、US は USD/JPY で換算。"""
    if close is None:
        return None
    try:
        price = float(close)
    except (TypeError, ValueError):
        return None
    if math.isnan(price) or price <= 0:
        return None
    if (market or "").upper() == "JP":
        return price
    return price * usdjpy


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
    """保有銘柄の利確ターゲットをHTML文字列で構築する（二段表示）。"""
    eff_pct = h.get("exit_effective_target_pct")
    if eff_pct is not None and cp > 0:
        eff_price = float(h.get("exit_effective_target_price") or 0)
        time_pct = h.get("exit_time_decay_target_pct")
        time_price = h.get("exit_time_decay_target_price")
        res_pct = h.get("exit_resistance_cap_pct")
        res_price = h.get("exit_resistance_cap_price")
        res_level = h.get("exit_resistance_level")
        binding = h.get("exit_target_binding") or "time_decay"
        entry = float(h.get("entry_price") or 0)
        pnl_pct = (cp - entry) / entry * 100 if entry > 0 else 0

        parts = ["<span class='tp-type-label'>シンプル利確</span>"]

        def _line(
            key: str,
            label: str,
            pct,
            price,
            hint: str = "",
        ) -> None:
            if pct is None or not price:
                return
            dist = (float(price) - cp) / cp * 100
            reached = pnl_pct >= float(pct)
            is_binding = binding == key
            if reached:
                css = "tp-reached"
                marker = "&#10003; "
            elif is_binding:
                css = "tp-active"
                marker = "&#8594; "
            else:
                css = "tp-pending"
                marker = ""
            hint_html = f"{hint} " if hint else ""
            parts.append(
                f"<span class='{css}'>{marker}{label}: +{float(pct):g}% "
                f"{float(price):,.0f} "
                f"<span class='tp-sub'>（{hint_html}あと{dist:+.1f}%）</span></span>"
            )

        _line("time_decay", "A・時間減衰", time_pct, time_price)
        if h.get("exit_resistance_cap_active") and res_pct is not None:
            res_hint = f"レジ {float(res_level):,.0f} / " if res_level else ""
            _line("resistance", "B・レジキャップ", res_pct, res_price, res_hint)

        if binding == "resistance" and h.get("exit_resistance_cap_active"):
            parts.append(
                "<span class='tp-sub'>有効目標: B（レジスタンス・キャップ）</span>"
            )

        if h.get("exit_extend_active"):
            parts.append("<span class='tp-active'>&#8594; 延長保有中</span>")
        sell_q = float(h.get("exit_sell_qty") or 0)
        if sell_q > 0:
            parts.append(f"<span class='tp-active'>&#8594; 推奨売却 {sell_q:.0f}株</span>")
        return "<br>".join(parts)

    try:
        from src.exit_simple import is_exit_simple_enabled
        if is_exit_simple_enabled():
            return "—"
    except Exception:
        pass

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
    ("cond_c_weekly_uptrend", "EMA25 > EMA75"),
    ("cond_c_rsi_oversold",   "RSI ≤ 35"),
    ("cond_c_sar_reversal",   "MACD確認"),
]
_SWING_COND_LABELS = _COND_C_LABELS


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
    score_trend_map: dict = {}

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
                   t.ema25, t.ema75, t.resistance_level,
                   d.final_score, d.recommend_rank,
                   d.trend_score, d.pattern_a_score, d.pattern_b_score, d.pattern_c_score, d.liquidity_score,
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
        # 買い候補カード用: 最新バッチの候補を詳細データ付きで取得
        buy_candidates = fetch_all(
            conn,
            """
            SELECT tsl.ticker, tsl.name, tsl.market, tsl.pattern,
                   tsl.entry_price, tsl.stop_loss_price, tsl.stop_loss_pct,
                   tsl.target_price, tsl.recommended_amount, tsl.signal_date,
                   tsl.conditions_met,
                   d.final_score, d.recommend_rank,
                   d.trend_score, d.pattern_a_score, d.pattern_b_score,
                   d.pattern_c_score, d.liquidity_score,
                   d.cond_a_ma200_rising, d.cond_a_ma_aligned, d.cond_a_52w_high_near,
                   d.cond_a_volume_surge, d.cond_a_rsi_ok, d.cond_a_above_ma25,
                   d.cond_b_ma200_rising, d.cond_b_ma_aligned, d.cond_b_pullback_touched,
                   d.cond_b_bounced, d.cond_b_ma25_rising, d.cond_b_rsi_ok,
                   d.cond_c_weekly_uptrend, d.cond_c_at_support, d.cond_c_rsi_oversold,
                   d.cond_c_sar_reversal
            FROM trade_signal_log tsl
            LEFT JOIN daily_signals d ON d.ticker = tsl.ticker AND d.date = tsl.signal_date
            WHERE tsl.signal_date = (
                SELECT MAX(signal_date) FROM trade_signal_log WHERE signal_date <= ?
            )
            ORDER BY d.final_score DESC, tsl.ticker
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

        from src.score_trend import fetch_score_trends_for_tickers

        _trend_tickers = list({
            dict(h)["ticker"] for h in holdings
        } | {
            dict(bc)["ticker"] for bc in buy_candidates
        })
        score_trend_map = fetch_score_trends_for_tickers(conn, _trend_tickers)

    # 最新ポートフォリオスナップショット
    try:
        from src.portfolio_snapshot import get_latest_snapshot
        latest_snapshot = get_latest_snapshot()
    except Exception:
        latest_snapshot = None

    # 保有銘柄の評価（exit_simple 利確目標・状態判定など）
    holdings_eval_map: dict = {}
    try:
        from src.holdings_monitor import evaluate_holdings as _evaluate_holdings
        for _r in _evaluate_holdings(date.today()):
            holdings_eval_map[_r["ticker"]] = _r
    except Exception as _eval_err:
        logger.debug(f"evaluate_holdings の呼び出しに失敗（report は継続表示）: {_eval_err}")

    return _render_report(
        today_str, market_env, holdings, candidates, market_history,
        llm_feedback, last_batch, data_insufficient,
        snapshot=latest_snapshot, buy_candidates=buy_candidates,
        holdings_eval_map=holdings_eval_map,
        score_trend_map=score_trend_map,
    )


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

        from src.score_trend import (
            compute_score_trend,
            fetch_score_history,
            format_score_trend_badge,
        )

        score_history = fetch_score_history(conn, ticker, limit=14)
        score_trend = compute_score_trend([h["final_score"] for h in score_history])

    return _render_stock_detail(
        ticker, master, tech, price, prev_price, range_52w,
        score, signals, fundamentals, risk_data,
        score_trend=score_trend,
        score_history=score_history,
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
                      d.cond_c_weekly_uptrend, d.cond_c_rsi_oversold, d.cond_c_sar_reversal,
                      d.macd_confirm_type,
                      d.close, d.rsi14, d.atr14, d.vol_ratio, d.rs_rank, d.market_mode,
                      t.macd_diff, t.macd_dea, t.macd_hist,
                      t.macd_diff_prev, t.macd_dea_prev, t.macd_hist_prev
               FROM daily_signals d
               LEFT JOIN universe_master u ON u.ticker = d.ticker
               LEFT JOIN technical_snapshot t ON t.ticker = d.ticker AND t.date = d.date
               WHERE (d.market, d.date) IN (
                   SELECT market, MAX(date) FROM daily_signals GROUP BY market
               )
               ORDER BY
                 CASE d.label WHEN 'candidate' THEN 0 WHEN 'watch' THEN 1 ELSE 2 END,
                 d.rsi14 IS NULL, d.rsi14 ASC,
                 d.ticker""",
        )

    # 表示用日付文字列
    if len(target_dates) == 1:
        display_date = target_dates[0]
    else:
        parts = [f"{m}: {d}" for m, d in sorted(date_map.items())]
        display_date = " / ".join(parts)

    return _render_screening(display_date, [dict(r) for r in rows])




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


register_api_routes(
    app,
    project_root=_PROJECT_ROOT,
    get_usdjpy=_get_usdjpy,
    get_or_calc_risk=_get_or_calc_risk,
)


# ============================================================
# HTML レンダリング: /report
# ============================================================


def _build_candidate_cards(
    buy_candidates: list[dict],
    mode: str,
    max_per_market: int = 3,
    score_trend_map: dict | None = None,
) -> list[dict]:
    """買い候補カード用のテンプレートデータを構築する。JP/US各最大 max_per_market 件。"""
    from src.score_trend import format_score_cell_html

    score_trend_map = score_trend_map or {}
    # スコア降順で JP/US それぞれ上位 max_per_market 件に絞る
    jp_count = us_count = 0
    filtered: list[dict] = []
    for bc in buy_candidates:
        mkt = bc.get("market") or "JP"
        if mkt == "JP":
            if jp_count >= max_per_market:
                continue
            jp_count += 1
        else:
            if us_count >= max_per_market:
                continue
            us_count += 1
        filtered.append(bc)

    cards: list[dict] = []
    for bc in filtered:
        pattern = bc.get("pattern") or ""
        entry = float(bc["entry_price"])
        sl_pct = float(bc["stop_loss_pct"] or 0)
        tg_pct = sl_pct * 2
        market = bc.get("market") or "JP"
        currency = "円" if market == "JP" else "USD"

        # 推奨投入額・株数
        amt = bc.get("recommended_amount")
        amt_str = f"{amt:,.0f} {currency}" if amt else "—"
        if amt and entry > 0:
            est_qty = amt / entry
            qty_str = f"約{est_qty:.0f}株" if est_qty >= 1 else f"約{est_qty:.2f}株"
        else:
            qty_str = "—"

        # スコア・ランク
        fscore = bc.get("final_score")
        score_pct = min(int(fscore), 100) if fscore is not None else 0
        score_str = f"{fscore:.0f}" if fscore is not None else "—"
        score_html = format_score_cell_html(
            float(fscore) if fscore is not None else None,
            score_trend_map.get(bc["ticker"]),
            score_str=score_str,
        )
        frank = bc.get("recommend_rank") or "—"
        rank_css = _RANK_CSS.get(frank, "rank-default")

        # 候補種別（conditions_met JSON から判定）
        cond_json = bc.get("conditions_met") or ""
        if cond_json and "❌" in str(cond_json):
            source_label = "スコア候補"
            source_css = "source-score"
        elif cond_json:
            source_label = "全条件一致"
            source_css = "source-strict"
        else:
            source_label = "—"
            source_css = ""

        # 条件ドット HTML
        cond_labels_map = {"A": _COND_A_LABELS, "B": _COND_B_LABELS, "C": _COND_C_LABELS}
        cond_labels = cond_labels_map.get(pattern, _COND_A_LABELS)
        conds_html = ""
        for col, tip in cond_labels:
            conds_html += _cond_dot(bc.get(col), tip)

        cards.append({
            "ticker": bc["ticker"],
            "name": bc.get("name") or "",
            "market": market,
            "pattern": pattern,
            "pattern_name": _PATTERN_NAME.get(pattern, pattern),
            "source_label": source_label,
            "source_css": source_css,
            "score_pct": score_pct,
            "score_str": score_str,
            "score_html": score_html,
            "rank": frank,
            "rank_css": rank_css,
            "entry_str": f"{entry:,.2f}",
            "sl_str": f"{float(bc['stop_loss_price']):,.2f}（-{sl_pct:.1f}%）",
            "target_str": f"{float(bc['target_price']):,.2f}（+{tg_pct:.1f}%）",
            "amt_str": amt_str,
            "qty_str": qty_str,
            "conditions_html": conds_html,
        })
    return cards


def _render_report(today_str, market_env, holdings, candidates, market_history, llm_feedback=None, last_batch=None, data_insufficient=0, snapshot=None, buy_candidates=None, holdings_eval_map=None, score_trend_map=None):
    from src.score_trend import format_score_cell_html

    market_env = dict(market_env) if market_env else {}
    holdings = [dict(h) for h in holdings]
    holdings_eval_map = holdings_eval_map or {}
    score_trend_map = score_trend_map or {}
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
    _BATCH_TYPE_LABEL = {
        "evening_jp": "夜バッチ(JP)",
        "morning_all": "朝バッチ(全体)",
        "monthly": "月次バッチ",
        "watchlist_hourly": "毎時バッチ",
    }
    if last_batch:
        lb = dict(last_batch)
        batch_label = _BATCH_TYPE_LABEL.get(lb.get("batch_type", ""), lb.get("batch_type", ""))
        batch_time = lb.get("end_time", "不明")
        batch_status = lb.get("status", "")
        batch_status_mark = "✅" if batch_status == "success" else "⚠️"
        freshness_html = f'<div class="freshness">最終バッチ: {batch_label} {batch_status_mark} {escape(str(batch_time))}</div>'
    else:
        freshness_html = '<div class="freshness-warn">⚠ バッチ実行履歴なし</div>'

    try:
        from src.exit_simple import is_exit_simple_enabled
        _simple_exit_global = is_exit_simple_enabled()
    except Exception:
        _simple_exit_global = False

    # --- 今日の行動セクション構築 ---
    action_items = []
    for _ha in holdings:
        h = dict(_ha)
        _eval = holdings_eval_map.get(h["ticker"]) or {}
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
        _eval_label = _eval.get("label", "")
        if _simple_exit_global and _eval_label in (
            "tp_full", "tp_partial", "tp_extend_hold", "stale_watch", "stale_exit",
        ):
            _sell_n = _eval.get("exit_sell_qty") or 0
            _sell_str = f"{_sell_n:.0f}株売却" if _sell_n > 0 else "売却検討"
            action_items.append((
                "info",
                f"🎯 {h['ticker']} {h.get('name', '')} — "
                f"{_eval_label}。{_sell_str}を検討",
            ))
        elif not _simple_exit_global:
            _pt = _eval.get("profit_type") or h.get("profit_type")
            if _pt in ("I", "II") and cp > 0:
                from src.take_profit import _split_quantity
                _r_val = float(_eval.get("r_value") or h.get("r_value") or 0)
                _qty_total = int(float(h.get("quantity") or 1)) or 1
                _t1_pct = float(_eval.get("t1_pct") or h.get("t1_pct") or 50)
                _t2_pct = float(_eval.get("t2_pct") or h.get("t2_pct") or 30)
                _t3_pct = float(_eval.get("t3_pct") or h.get("t3_pct") or 20)
                _sq1, _sq2, _sq3 = _split_quantity(_qty_total, _t1_pct, _t2_pct, _t3_pct)
                _sell_map = {"t1": _sq1, "t2": _sq2, "t3": _sq3}
                for stage, label_str in [("t1", "T1"), ("t2", "T2"), ("t3", "T3")]:
                    t_price = _eval.get(f"tp_{stage}_price") or h.get(f"{stage}_price")
                    t_reached = _eval.get(f"tp_{stage}_reached") or h.get(f"{stage}_reached")
                    if t_price and not t_reached and cp >= t_price:
                        _r_mult = f"+{(t_price - ep) / _r_val:.1f}R" if _r_val > 0 else ""
                        _sell_n = _sell_map.get(stage, 0)
                        _sell_str = f"{_sell_n}株売却" if _sell_n > 0 else "売却検討"
                        action_items.append((
                            "info",
                            f"🎯 {h['ticker']} {h.get('name', '')} — "
                            f"{label_str}到達（{_r_mult}）。{_sell_str}を検討"
                        ))
                _adj = _eval.get("tp_adjusted_stop") or h.get("adjusted_stop")
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
    remaining_slots = _MAX_HOLDINGS - len(holdings)
    if remaining_slots <= 0:
        action_items.append(("warn", f"🈵 保有枠が満杯です（{len(holdings)}/{_MAX_HOLDINGS}）— 新規エントリーは既存ポジション整理後に検討してください"))
    else:
        action_items.append(("info", f"📦 保有枠: {len(holdings)}/{_MAX_HOLDINGS}（残り{remaining_slots}枠）"))

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
    # evaluate_holdings() の出口・利確情報を行に付与する。
    try:
        from src.config import is_offensive as _is_offensive_mode
        _offensive_now = bool(_is_offensive_mode())
    except Exception:
        _offensive_now = False

    holding_rows = []
    for _h in holdings:
        h = dict(_h)  # sqlite3.Row → dict（.get() を使えるようにする）
        _eval = holdings_eval_map.get(h["ticker"]) or {}
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
        entry_str = f"@{ep:,.2f}" if ep else ""

        # 利確ターゲット表示（exit_simple 優先）
        target_str = "—"
        target_html = "—"
        h_merged = {**h, **_eval}
        if _simple_exit_global and _eval.get("exit_effective_target_pct") is not None and cp:
            target_html = _build_tp_target_html(h_merged, cp)
        elif _simple_exit_global and cp and ep > 0:
            try:
                from src.exit_simple import resolve_exit_targets
                holding_days = _eval.get("holding_days")
                if holding_days is None and h.get("entry_date"):
                    entry_dt = datetime.strptime(h["entry_date"], "%Y-%m-%d").date()
                    holding_days = (date.today() - entry_dt).days
                _targets = resolve_exit_targets(
                    ep, holding_days, h.get("resistance_level"),
                )
                h_merged.update({
                    "exit_effective_target_pct": _targets.get("effective_target_pct"),
                    "exit_effective_target_price": _targets.get("effective_target_price"),
                    "exit_time_decay_target_pct": _targets.get("time_decay_target_pct"),
                    "exit_time_decay_target_price": _targets.get("time_decay_target_price"),
                    "exit_resistance_cap_pct": _targets.get("resistance_cap_pct"),
                    "exit_resistance_cap_price": _targets.get("resistance_cap_price"),
                    "exit_resistance_level": _targets.get("resistance_level"),
                    "exit_target_binding": _targets.get("target_binding"),
                    "exit_resistance_cap_active": _targets.get("resistance_cap_active"),
                    "exit_extend_active": bool(_eval.get("exit_extend_active")),
                    "exit_sell_qty": _eval.get("exit_sell_qty") or 0,
                })
                target_html = _build_tp_target_html(h_merged, cp)
            except Exception:
                target_html = "—"
        elif not _simple_exit_global:
            profit_type = _eval.get("profit_type") or h.get("profit_type") or "III"
            if profit_type in ("I", "II") and cp:
                target_html = _build_tp_target_html({**h, **_eval}, cp)
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
        score_html = format_score_cell_html(
            float(fscore) if fscore is not None else None,
            score_trend_map.get(h["ticker"]),
            score_str=score_str,
        )
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
        _exit_label = _eval.get("label", "")
        _any_tp_reached = _exit_label in (
            "tp_full", "tp_partial", "tp_extend_hold", "tp_extend_exit", "stale_exit",
        ) if _simple_exit_global else (
            (_eval.get("profit_type") or h.get("profit_type")) in ("I", "II")
            and cp
            and (
                ((_eval.get("tp_t1_price") or h.get("t1_price"))
                 and not (_eval.get("tp_t1_reached") or h.get("t1_reached"))
                 and cp >= float(_eval.get("tp_t1_price") or h.get("t1_price") or 0))
                or ((_eval.get("tp_t2_price") or h.get("t2_price"))
                    and not (_eval.get("tp_t2_reached") or h.get("t2_reached"))
                    and cp >= float(_eval.get("tp_t2_price") or h.get("t2_price") or 0))
                or ((_eval.get("tp_t3_price") or h.get("t3_price"))
                    and not (_eval.get("tp_t3_reached") or h.get("t3_reached"))
                    and cp >= float(_eval.get("tp_t3_price") or h.get("t3_price") or 0))
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

        # --- 攻めモード追加シグナル（add_buy / tp_trailing_level / tp_double_milestone） ---
        offensive_badges: list[str] = []
        offensive_tips: list[str] = []
        add_buy_sig = _eval.get("add_buy_signal")
        if add_buy_sig and add_buy_sig.get("eligible"):
            offensive_badges.append(
                "<span class='off-badge off-badge-addbuy' title='買い増し候補（攻めモード）'>📈 買増</span>"
            )
            reasons = add_buy_sig.get("reasons") or []
            if reasons:
                offensive_tips.append("買い増し: " + " / ".join(reasons))
        tp_trail = _eval.get("tp_trailing_level")
        if tp_trail:
            label_map = {"ma25": "MA25トレ", "ma50": "MA50撤退"}
            label_jp = label_map.get(str(tp_trail), str(tp_trail))
            css_cls = "off-badge-ma50" if tp_trail == "ma50" else "off-badge-ma25"
            offensive_badges.append(
                f"<span class='off-badge {css_cls}' title='トレーリング段階'>📉 {label_jp}</span>"
            )
        if _eval.get("tp_double_milestone"):
            dbl_qty = _eval.get("tp_double_sell_qty") or 0
            qty_txt = f"（{dbl_qty:.0f}株）" if dbl_qty else ""
            offensive_badges.append(
                f"<span class='off-badge off-badge-double' title='株価2倍特例の発動'>💰 2倍特例{qty_txt}</span>"
            )
        offensive_html = " ".join(offensive_badges) if offensive_badges else (
            "<span class='text-muted-custom'>—</span>" if _offensive_now else ""
        )
        offensive_title = " | ".join(offensive_tips) if offensive_tips else ""

        holding_rows.append({
            "ticker": h["ticker"],
            "name": h.get("name") or "",
            "cp_str": cp_str,
            "entry_str": entry_str,
            "pnl_css": pnl_color,
            "pnl_str": pnl_str,
            "pnl_jpy_str": pnl_jpy_str,
            "status_cls": status_cls,
            "status_label": status_label,
            "sl_str": f"{sl:,.2f}",
            "sl_raw": sl,
            "adjusted_stop": (
                float(_eval["tp_adjusted_stop"])
                if _eval.get("tp_adjusted_stop") else None
            ),
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
            "score_html": score_html,
            "rank_html": rank_html,
            "trend_str": trend_str,
            "offensive_html": offensive_html,
            "offensive_title": offensive_title,
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

    # --- 買い候補カード構築 ---
    buy_cands = [dict(b) for b in buy_candidates] if buy_candidates else []
    buy_candidate_cards = _build_candidate_cards(buy_cands, mode, score_trend_map=score_trend_map)
    buy_candidate_date = buy_cands[0].get("signal_date", "") if buy_cands else ""
    buy_candidate_is_today = (buy_candidate_date == today_str)
    is_market_caution = mode in ("caution", "full_caution")

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
        usdjpy_rate=usdjpy,
        total_assets=total_assets,
        total_assets_source=total_assets_source,
        total_cash_jpy_equiv=total_cash_jpy_equiv,
        buy_candidate_cards=buy_candidate_cards,
        buy_candidate_date=buy_candidate_date,
        buy_candidate_is_today=buy_candidate_is_today,
        is_market_caution=is_market_caution,
        is_offensive_mode=_offensive_now,
    )


# ============================================================
# HTML レンダリング: /stock/<ticker>
# ============================================================

def _render_stock_detail(ticker, master, tech, price, prev_price, range_52w,
                         score, signals, fundamentals, risk_data,
                         score_trend=None, score_history=None):
    from src.score_trend import format_score_trend_badge, score_trend_label
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
    score_trend_badge = format_score_trend_badge(score_trend or {})
    score_trend_label_jp = score_trend_label((score_trend or {}).get("direction", "unknown"))
    score_history_rows = [
        {"date": h["date"], "score": f"{h['final_score']:.0f}"}
        for h in (score_history or [])[-8:]
    ]

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
        score_trend_badge=score_trend_badge,
        score_trend_label=score_trend_label_jp,
        score_history_rows=score_history_rows,
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


def _bookmark_btn_html(ticker: str, name: str, market: str, bookmarked: bool) -> str:
    """Screening 行用のブックマークボタン HTML。"""
    css = "bookmark-btn bookmark-btn-on" if bookmarked else "bookmark-btn"
    star = "★" if bookmarked else "☆"
    title = "ブックマーク解除" if bookmarked else "ブックマークに追加"
    return (
        f'<button type="button" class="{css}" data-ticker="{escape(ticker)}"'
        f' data-name="{escape(name)}" data-market="{escape(market)}"'
        f' title="{title}" aria-label="{title}" aria-pressed="{"true" if bookmarked else "false"}">'
        f"{star}</button>"
    )


def _swing_cond_badge(
    value: int | bool | None,
    short: str,
    tip: str,
    extra_css: str = "",
) -> str:
    """スイング条件をバッジで描画する。"""
    met = value == 1 or value is True
    css = "swing-cond-met" if met else "swing-cond-unmet"
    if extra_css:
        css = f"{css} {extra_css}"
    mark = "✓" if met else "—"
    return (
        f'<span class="swing-cond-badge {css} tip" data-tip="{escape(tip)}">'
        f"{mark} {escape(short)}</span>"
    )


def _macd_cond_badges_html(row: dict) -> str:
    """MACD 条件をヒスト反転 / GC の2バッジで表示する。"""
    from src.swing_detector import macd_confirm_flags

    hist_ok, gc_ok = macd_confirm_flags(row)
    hist_tip = (
        "ヒストグラム反転: 前日ヒスト≤0 かつ 当日ヒストが前日より上向き"
        "（DIFがDEAを上抜けていなくても可）"
    )
    gc_tip = "MACDゴールデンクロス: 前日 DIF≤DEA かつ 当日 DIF>DEA"
    return (
        _swing_cond_badge(hist_ok, "ヒスト", hist_tip, "swing-cond-macd-hist")
        + _swing_cond_badge(gc_ok, "GC", gc_tip, "swing-cond-macd-gc")
    )


def _render_screening(target_date: str, rows: list[dict]) -> str:
    from src.config import get_swing_config
    from src.screening_bookmarks import get_bookmark_tickers

    usdjpy = _get_usdjpy()
    swing_cfg = get_swing_config()
    rsi_max = int(float(swing_cfg.get("rsi_max", 35)))
    bookmark_tickers = get_bookmark_tickers()
    total = len(rows)
    cand_cnt = sum(1 for r in rows if r["label"] == "candidate")
    watch_cnt = sum(1 for r in rows if r["label"] == "watch")
    skip_cnt = sum(1 for r in rows if r["label"] == "skip")
    funnel_uptrend = sum(1 for r in rows if r.get("cond_c_weekly_uptrend") == 1)
    funnel_rsi = sum(1 for r in rows if r.get("cond_c_rsi_oversold") == 1)
    funnel_macd = sum(1 for r in rows if r.get("cond_c_sar_reversal") == 1)

    table_rows = []
    for r in rows:
        ticker = r["ticker"] or ""
        name = r.get("name") or ""
        market = r.get("market") or ""
        label = r.get("label") or "skip"
        close = r.get("close")
        price_jpy = _close_price_jpy(close, market, usdjpy)
        rsi = r.get("rsi14")
        vol_ratio = r.get("vol_ratio")
        sector = r.get("sector") or "—"

        uptrend = r.get("cond_c_weekly_uptrend") == 1
        rsi_ok = r.get("cond_c_rsi_oversold") == 1
        macd_ok = r.get("cond_c_sar_reversal") == 1
        from src.swing_detector import macd_confirm_flags

        hist_ok, gc_ok = macd_confirm_flags(r)
        met_count = sum((uptrend, rsi_ok, macd_ok))

        lbl_css = _LABEL_CSS.get(label, "label-skip")
        lbl_text = _LABEL_DISPLAY.get(label, label)

        conds_html = (
            _swing_cond_badge(uptrend, "トレンド", "EMA25 > EMA75（上昇トレンド）")
            + _swing_cond_badge(rsi_ok, f"RSI≤{rsi_max}", f"RSI ≤ {rsi_max}")
            + _macd_cond_badges_html(r)
        )

        met_css = "swing-met-full" if met_count == 3 else (
            "swing-met-partial" if met_count == 2 else "swing-met-low"
        )
        met_html = f'<span class="swing-met-count {met_css}">{met_count}/3</span>'

        if label == "candidate":
            verdict_html = '<span class="verdict-candidate">買い候補</span>'
        elif label == "watch":
            verdict_html = '<span class="verdict-watch">あと1条件</span>'
        else:
            verdict_html = '<span class="verdict-skip">対象外</span>'

        if price_jpy is not None:
            price_jpy_rounded = int(round(price_jpy))
            price_jpy_str = f"{price_jpy_rounded:,}円"
            price_jpy_attr = str(price_jpy_rounded)
        else:
            price_jpy_str = "—"
            price_jpy_attr = ""

        close_str = f"{close:,.2f}" if close else "—"
        rsi_str = f"{rsi:.0f}" if rsi is not None else "—"
        rsi_css = ""
        if rsi is not None:
            if rsi > rsi_max:
                rsi_css = "rsi-high"
            elif rsi <= rsi_max:
                rsi_css = "rsi-low"
        vr_str = f"{vol_ratio:.1f}x" if vol_ratio is not None else "—"

        is_bm = ticker in bookmark_tickers
        bookmark_html = _bookmark_btn_html(ticker, name, market, is_bm)

        prompt_href = f"/prompt-builder?from=screening&theme=buy&tickers={escape(ticker)}"
        row_html = (
            f'<tr data-ticker="{escape(ticker)}" data-label="{escape(label)}"'
            f' data-market="{escape(market)}"'
            f' data-uptrend="{"1" if uptrend else "0"}"'
            f' data-rsi-ok="{"1" if rsi_ok else "0"}"'
            f' data-macd-ok="{"1" if macd_ok else "0"}"'
            f' data-macd-hist="{"1" if hist_ok else "0"}"'
            f' data-macd-gc="{"1" if gc_ok else "0"}"'
            f' data-cond-met="{met_count}"'
            f' data-rsi="{rsi if rsi is not None else ""}"'
            f' data-price-jpy="{escape(price_jpy_attr)}"'
            f' data-bookmarked="{"1" if is_bm else "0"}">'
            f'<td class="bookmark-cell">{bookmark_html}</td>'
            f'<td class="screening-ticker-cell">'
            f'<a class="ticker-link" href="/stock/{escape(ticker)}">{escape(ticker)}</a>'
            f'<a class="screening-prompt-link tip" href="{prompt_href}"'
            f' data-tip="Prompt Builder で購入相談プロンプトを作成">Prompt</a>'
            f"</td>"
            f'<td class="mobile-hide">{escape(name)}</td>'
            f'<td class="mobile-hide"><span class="market-pill market-{escape(market.lower())}">{escape(market)}</span></td>'
            f'<td><span class="rank-badge {lbl_css}">{lbl_text}</span></td>'
            f'<td class="swing-cond-col">{conds_html}</td>'
            f'<td class="text-center">{met_html}</td>'
            f'<td class="text-center rsi-col {rsi_css}"><strong>{rsi_str}</strong></td>'
            f'<td>{verdict_html}</td>'
            f'<td class="price-jpy-col text-end">{price_jpy_str}</td>'
            f'<td class="detail-col">{close_str}</td>'
            f'<td class="detail-col">{vr_str}</td>'
            f'<td class="detail-col mobile-hide">{escape(sector)}</td>'
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
        funnel_uptrend=funnel_uptrend,
        funnel_rsi=funnel_rsi,
        funnel_macd=funnel_macd,
        swing_rsi_max=rsi_max,
        bookmark_cnt=len(bookmark_tickers),
        usdjpy_rate=usdjpy,
        table_rows=table_rows,
    )


# ============================================================
# ルーティング: /settings（設定ページ）
# ============================================================

# ダッシュボードから編集可能な設定項目の定義
# (section, key, label, type, default, min, max, step, help)

# ============================================================
# 投資ルール確認ページ
# ============================================================

@app.route("/rules")
def rules_page():
    """初心者向けに、システムが守る投資ルールを一覧表示する。"""
    context = _build_rules_context()
    return render_template("rules.html", active_page="rules", **context)


@app.route("/scoring")
@app.route("/soring")
def scoring_typo_redirect():
    return redirect(url_for("rules_page"), code=302)


def _build_rules_context() -> dict:
    from src.config import get_exit_simple_config, get_swing_config

    cap = CONFIG.get("capital", {})
    sig = CONFIG.get("signal", {})
    screening = CONFIG.get("screening", {})
    goal = CONFIG.get("investment_goal", {})
    risk = CONFIG.get("risk_control", {})
    paper = CONFIG.get("paper_mode", {})
    swing = get_swing_config()
    exit_cfg = get_exit_simple_config()

    monthly_deposit = _cfg_float(goal, "monthly_deposit_jpy", 10_000)
    target_return = _cfg_float(goal, "target_monthly_return_pct", 15.0)
    risk_pct = _cfg_float(swing, "max_risk_per_trade_pct", _cfg_float(cap, "max_risk_per_trade_pct", 2.0))
    max_holdings = _cfg_int(cap, "max_concurrent_holdings", 3)
    rr_ratio = _cfg_float(swing, "risk_reward_ratio", _cfg_float(sig, "risk_reward_ratio", 2.0))

    mode_label = "📈 EMA押し目スイング"
    status_cards = [
        {"label": "戦略", "value": mode_label, "tone": "info"},
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
                f"米国株は ${swing.get('us_min_price', 10):.0f}〜${swing.get('us_max_price', 50):.0f}、日本株は ¥{swing.get('jp_min_price', 300):.0f}〜¥{swing.get('jp_max_price', 3000):.0f} の株価帯に限定します。",
                "流動性（売買代金・時価総額・直近30日の取引日数）で足切りします。",
            ],
        },
        {
            "title": "シグナル条件（スイング）",
            "icon": "activity",
            "rules": [
                f"監視リスト: EMA{swing.get('ema_short', 25)} > EMA{swing.get('ema_long', 75)}（上昇トレンド）。",
                f"候補: RSI ≤ {swing.get('rsi_max', 35)}（直近{swing.get('rsi_lookback_days', 5)}日以内可）+ MACD反転またはゴールデンクロス。",
                f"1バッチの通知上限は {_cfg_int(sig, 'max_entry_sets_per_batch', 3)}件です。",
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
                f"損切りはサポートライン下（バッファ {_cfg_float(swing, 'support_stop_buffer_pct', 1.0):.1f}%）を優先します。",
                f"サポートが不明な場合は ATR x {_cfg_float(sig, 'atr_stop_multiplier', 2.0):.1f} を使います。",
                (
                    f"利確目標A（時間減衰）: 保有日数で段階的に低下（"
                    f"+{_cfg_float(exit_cfg, 'initial_target_pct', 20):.0f}%"
                    f"→+{_cfg_float(exit_cfg, 'initial_target_pct', 20) - _cfg_float(exit_cfg, 'target_decay_pct', 5):.0f}%"
                    f"→+{_cfg_float(exit_cfg, 'initial_target_pct', 20) - 2 * _cfg_float(exit_cfg, 'target_decay_pct', 5):.0f}%"
                    f"→+{_cfg_float(exit_cfg, 'target_floor_pct', 5):.0f}%、"
                    f"{_cfg_int(exit_cfg, 'target_decay_every_days', 30)}日ごと）。"
                ),
                (
                    "利確目標B（レジスタンス・キャップ）: 推定レジスタンスの手前で目標%を引き下げ。"
                    f"バッファ {_cfg_float(exit_cfg.get('resistance_cap', {}), 'buffer_pct', 1.5):.1f}%、"
                    f"適用範囲 +{_cfg_float(exit_cfg.get('resistance_cap', {}), 'min_reward_pct', 8):.0f}%〜"
                    f"+{_cfg_float(exit_cfg.get('resistance_cap', {}), 'ignore_if_above_pct', 25):.0f}%。"
                    "AとBのうち近い方が有効目標。"
                ),
                (
                    f"目標到達時: 延長条件（EMA25>EMA75 / MA25上 / RSI≤"
                    f"{_cfg_float(exit_cfg, 'rsi_max_for_hold', 70):.0f}）を満たせば2株以上は半分売却・1株は温存。"
                    "満たさなければ全売却を推奨します。"
                ),
                (
                    f"保有{_cfg_int(exit_cfg, 'stale_days_soft', 90)}日超かつ"
                    f"+{_cfg_float(exit_cfg, 'stale_min_pnl_pct', 5):.0f}%未満は売却検討、"
                    f"{_cfg_int(exit_cfg, 'stale_days_hard', 120)}日超かつ"
                    f"+{_cfg_float(exit_cfg, 'stale_hard_min_pnl_pct', 10):.0f}%未満（または含み損）は全売推奨。"
                ),
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


# ============================================================
# 設定ページ
# ============================================================

@app.route("/settings")
def settings_page():
    """設定ページ"""
    return render_template("settings.html", active_page="settings")


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


def _enrich_holdings_for_prompt(rows) -> list[dict]:
    holdings = []
    for row in rows:
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
    return holdings


def _load_prompt_data(
    conn,
    today_str: str,
    screening_tickers: list[str] | None = None,
    *,
    prefer_screening: bool = False,
) -> tuple[list[dict], list[dict], dict]:
    """Prompt Builder 用の保有・候補・市場環境を読み込む。"""
    from src.prompt_screening import (
        count_open_holdings,
        enrich_tickers_from_daily_signals,
        merge_prompt_candidates,
    )

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

    holdings = _enrich_holdings_for_prompt(holdings_rows)
    candidates = _dedup_candidates([dict(r) for r in candidates_rows])

    if screening_tickers:
        screening = enrich_tickers_from_daily_signals(
            conn,
            screening_tickers,
            open_holdings_count=count_open_holdings(conn),
        )
        candidates = merge_prompt_candidates(
            candidates, screening, prefer_screening=prefer_screening,
        )

    me = dict(market_env) if market_env else {}
    return holdings, candidates, me


def _parse_screening_tickers_param(raw: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for part in (raw or "").split(","):
        t = part.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


@app.route("/prompt-builder")
def prompt_builder_page():
    """プロンプト作成ページ"""
    today_str = date.today().strftime("%Y-%m-%d")
    from_screening = request.args.get("from") == "screening"
    prefill_tickers = _parse_screening_tickers_param(request.args.get("tickers", ""))

    with get_connection() as conn:
        holdings, candidates, me = _load_prompt_data(
            conn,
            today_str,
            prefill_tickers if prefill_tickers else None,
            prefer_screening=from_screening,
        )

    from src.prompt_builder import THEMES

    prefill = {
        "from_screening": from_screening,
        "theme": request.args.get("theme", "buy"),
        "tickers": prefill_tickers,
    }
    return render_template(
        "prompt_builder.html",
        active_page="prompt_builder",
        holdings=holdings,
        candidates=candidates,
        market_env=me,
        themes=THEMES,
        prefill=prefill,
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
    options = dict(data.get("options") or {})
    today_str = date.today().strftime("%Y-%m-%d")
    from_screening = bool(options.get("from_screening"))

    with get_connection() as conn:
        holdings, candidates, me = _load_prompt_data(
            conn,
            today_str,
            tickers if tickers else None,
            prefer_screening=from_screening,
        )

    if from_screening:
        options["from_screening"] = True

    result = generate_prompt(theme, tickers, holdings, candidates, me, options)
    return jsonify({"success": True, **result})


# ============================================================
# エントリーポイント
# ============================================================

def main():
    logger.info(f"レポートサーバーを起動します: http://{_HOST}:{_PORT}/report")
    logger.info("  /report       - メインダッシュボード")
    logger.info("  /rules        - 投資ルール確認")
    logger.info("  /screening    - スクリーニング銘柄一覧")
    logger.info("  /performance  - パフォーマンス分析")
    logger.info("  /prompt-builder - プロンプト作成")
    logger.info("  /settings     - 設定")
    logger.info("  /stock/<ticker> - 個別銘柄詳細")
    logger.info("  Ctrl+C で停止")
    app.run(host=_HOST, port=_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
