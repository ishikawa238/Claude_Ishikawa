"""
Flask API ルート（order_server から分離）

register_api_routes() で app にエンドポイントを登録する。
"""

import csv
import json
import threading
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

from flask import Blueprint, Flask, jsonify, request

api_bp = Blueprint('api', __name__)

from src.config import CONFIG
from src.data_store import fetch_all, fetch_one, get_connection, insert_one
from src.logger import get_logger

from scripts.routes.settings_schema import EDITABLE_SETTINGS, GROUP_LABELS, GROUP_ORDER

logger = get_logger(__name__)

_PROJECT_ROOT = None
_CONFIG_PATH = None
_get_usdjpy: Callable[[], float] | None = None
_get_or_calc_risk: Callable | None = None

@dataclass
class _BatchTask:
    batch_type: str
    status: str = "running"
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


# ============================================================
# API エンドポイント群
# ============================================================

@api_bp.route("/api/portfolio-pnl")
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


@api_bp.route("/api/score-radar")
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


@api_bp.route("/api/price-history")
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


@api_bp.route("/api/market-timeline")
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


@api_bp.route("/api/screening-bookmarks", methods=["GET"])
def api_screening_bookmarks_get():
    """Screening ブックマーク一覧"""
    from src.screening_bookmarks import load_bookmarks

    items = load_bookmarks()
    return jsonify({
        "success": True,
        "tickers": [i["ticker"] for i in items],
        "items": items,
        "count": len(items),
    })


@api_bp.route("/api/screening-bookmarks/toggle", methods=["POST"])
def api_screening_bookmarks_toggle():
    """Screening ブックマークの追加/解除"""
    from src.screening_bookmarks import toggle_bookmark

    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"success": False, "error": "無効なリクエスト"}), 400

    ticker = (data.get("ticker") or "").strip()
    if not ticker:
        return jsonify({"success": False, "error": "ティッカーを指定してください"}), 400

    try:
        result = toggle_bookmark(
            ticker,
            name=data.get("name"),
            market=data.get("market"),
        )
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        logger.exception("ブックマーク切替エラー")
        return jsonify({"success": False, "error": str(e)}), 500

    msg = f"{ticker.upper()} をブックマーク{'に追加' if result['bookmarked'] else 'から解除'}しました"
    return jsonify({"success": True, "message": msg, **result})


@api_bp.route("/api/benchmark-comparison")
def api_benchmark_comparison():
    """ポートフォリオ総資産 vs S&P500 の正規化推移（ベンチマーク比較）"""
    days = int(request.args.get("days", 365))
    from src.benchmark_comparison import build_benchmark_comparison

    return jsonify(build_benchmark_comparison(days=days))


@api_bp.route("/api/perf-monthly")
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


@api_bp.route("/api/perf-cumulative")
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


@api_bp.route("/api/recent-signals")
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


@api_bp.route("/api/trade-result", methods=["POST"])
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


@api_bp.route("/api/holdings/update-sl", methods=["POST"])
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


@api_bp.route("/api/sector-alloc")
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


@api_bp.route("/api/portfolio-risk")
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


@api_bp.route("/api/system-health")
def api_system_health():
    """システムヘルス情報（最終バッチ・エラー数・データ鮮度）"""
    with get_connection() as conn:
        last_batch = fetch_one(
            conn,
            """SELECT batch_type, status, end_time, memo
               FROM batch_run_log ORDER BY end_time DESC LIMIT 1""",
        )
        last_jp_batch = fetch_one(
            conn,
            """SELECT status, memo, end_time
               FROM batch_run_log
               WHERE batch_type = 'evening_jp'
               ORDER BY end_time DESC LIMIT 1""",
        )
        error_count = fetch_one(
            conn,
            """SELECT COUNT(*) AS cnt FROM error_log
               WHERE error_time >= datetime('now', '-24 hours')""",
        )
        recent_errors = fetch_all(
            conn,
            """SELECT error_time, batch_type, module, error_type, error_message, is_fatal
               FROM error_log
               WHERE error_time >= datetime('now', '-24 hours')
               ORDER BY error_time DESC
               LIMIT 20""",
        )
        latest_price = fetch_one(
            conn,
            "SELECT MAX(date) AS d FROM price_history",
        )

    _BTYPES = {
        "evening_jp": "夜(JP)",
        "morning_all": "朝(全)",
        "monthly": "月次",
        "watchlist_hourly": "毎時",
    }
    result = {
        "last_batch": None,
        "errors_24h": 0,
        "recent_errors": [],
        "data_date": None,
        "jp_price_source": None,
        "jp_price_status": None,
    }
    if last_batch:
        lb = dict(last_batch)
        result["last_batch"] = {
            "type": _BTYPES.get(lb.get("batch_type", ""), lb.get("batch_type", "")),
            "status": lb.get("status", ""),
            "time": lb.get("end_time", ""),
        }
    if last_jp_batch:
        jb = dict(last_jp_batch)
        memo_raw = jb.get("memo") or ""
        if memo_raw:
            try:
                memo = json.loads(memo_raw)
                result["jp_price_source"] = memo.get("jp_price_source")
                result["jp_price_status"] = memo.get("jp_price_status")
            except json.JSONDecodeError:
                pass
        if result["jp_price_status"] is None and jb.get("status") == "partial_failure":
            result["jp_price_status"] = "partial"
    if error_count:
        result["errors_24h"] = dict(error_count).get("cnt", 0)
    if recent_errors:
        result["recent_errors"] = [
            {
                "time": row["error_time"],
                "batch": _BTYPES.get(row["batch_type"], row["batch_type"] or ""),
                "module": row["module"] or "",
                "type": row["error_type"] or "",
                "message": (row["error_message"] or "")[:500],
                "fatal": bool(row["is_fatal"]),
            }
            for row in recent_errors
        ]
    if latest_price:
        result["data_date"] = dict(latest_price).get("d")
    return jsonify(result)


@api_bp.route("/api/earnings-calendar")
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


@api_bp.route("/api/screening-funnel")
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

        cond_swing = fetch_one(
            conn,
            f"""SELECT COUNT(*) AS total,
                      SUM(cond_c_weekly_uptrend) AS uptrend,
                      SUM(cond_c_rsi_oversold) AS rsi_oversold,
                      SUM(cond_c_sar_reversal) AS macd_confirm
               FROM daily_signals
               WHERE {date_filter}""",
            date_params,
        )
        cond_swing = dict(cond_swing) if cond_swing else {}

        cond_met_rows = fetch_all(
            conn,
            f"""SELECT
                 (COALESCE(cond_c_weekly_uptrend, 0)
                  + COALESCE(cond_c_rsi_oversold, 0)
                  + COALESCE(cond_c_sar_reversal, 0)) AS met,
                 COUNT(*) AS cnt
               FROM daily_signals
               WHERE {date_filter}
               GROUP BY met
               ORDER BY met""",
            date_params,
        )
        cond_met_dist = {str(dict(r)["met"]): dict(r)["cnt"] for r in cond_met_rows}

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
    swing_total = cond_swing.get("total") or screened_cnt or 1
    return jsonify({
        "date": target,
        "strategy": "swing",
        "funnel": {
            "universe": universe_cnt,
            "screened": screened_cnt,
            "candidate": labels.get("candidate", 0),
            "watch": labels.get("watch", 0),
            "skip": labels.get("skip", 0),
            "notified": notified_cnt,
        },
        "by_market": by_market,
        "swing": {
            "total": swing_total,
            "uptrend": cond_swing.get("uptrend") or 0,
            "rsi_oversold": cond_swing.get("rsi_oversold") or 0,
            "macd_confirm": cond_swing.get("macd_confirm") or 0,
            "cond_met": cond_met_dist,
        },
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
# 設定 API
# ============================================================

def _get_config_value(section: str, key: str, default):
    """
    設定値を取得する。section は以下の3形式を扱う:
        "_top"          → CONFIG[key] （ルートキー）
        "section"       → CONFIG[section][key]
        "a.b.c..."      → CONFIG[a][b][c]...[key]（ネスト対応）
    """
    if section == "_top":
        return CONFIG.get(key, default)
    parts = section.split(".")
    obj = CONFIG
    for p in parts:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(p, {})
    if not isinstance(obj, dict):
        return default
    if section == "signal" and key == "max_entry_sets_per_batch":
        return obj.get(key, obj.get("max_candidates_per_pattern", default))
    return obj.get(key, default)


def _set_config_value(target: dict, section: str, key: str, value) -> None:
    """target 辞書に対し、section を辿って key を value にセットする。"""
    if section == "_top":
        target[key] = value
        return
    parts = section.split(".")
    obj = target
    for p in parts:
        if p not in obj or not isinstance(obj[p], dict):
            obj[p] = {}
        obj = obj[p]
    obj[key] = value


@api_bp.route("/api/settings", methods=["GET"])
def api_settings_get():
    """現在の設定値を返す"""
    result = []
    for item in EDITABLE_SETTINGS:
        current = _get_config_value(item["section"], item["key"], item["default"])
        entry = {
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
        }
        if item.get("choices"):
            entry["choices"] = item["choices"]
        result.append(entry)
    return jsonify({
        "settings": result,
        "group_labels": GROUP_LABELS,
        "group_order": GROUP_ORDER,
    })


@api_bp.route("/api/settings", methods=["POST"])
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
    allowed = {(s["section"], s["key"]): s for s in EDITABLE_SETTINGS}

    # バリデーション
    errors = []
    validated: list[tuple[str, str, object]] = []
    for compound_key, raw_value in updates.items():
        # 右から最後の '.' で分割（ネストした section に対応）
        if "." not in compound_key:
            errors.append(f"不正なキー形式: {compound_key}")
            continue
        section, key = compound_key.rsplit(".", 1)
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
            elif spec["type"] == "enum":
                value = str(raw_value)
                choices = [c["value"] for c in (spec.get("choices") or [])]
                if choices and value not in choices:
                    errors.append(
                        f"{spec['label']}: 値が選択肢にありません（{', '.join(choices)}）"
                    )
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
        _set_config_value(full_config, section, key, value)

    # ファイルに書き戻し
    try:
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(full_config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"config.json 書き込み失敗: {e}")
        return jsonify({"error": "config.json の書き込みに失敗しました"}), 500

    # メモリ上の CONFIG も同期
    for section, key, value in validated:
        _set_config_value(CONFIG, section, key, value)

    logger.info(f"設定を更新しました: {len(validated)} 項目")
    return jsonify({"success": True, "updated": len(validated)})



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


@api_bp.route("/api/batch/trigger", methods=["POST"])
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


@api_bp.route("/api/batch/status")
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


def register_api_routes(
    app: Flask,
    *,
    project_root,
    get_usdjpy: Callable[[], float],
    get_or_calc_risk: Callable,
) -> None:
    """API ルートを Flask app に登録する。"""
    global _PROJECT_ROOT, _get_usdjpy, _get_or_calc_risk, _CONFIG_PATH
    _PROJECT_ROOT = project_root
    _get_usdjpy = get_usdjpy
    _get_or_calc_risk = get_or_calc_risk
    _CONFIG_PATH = project_root / "config.json"
    app.register_blueprint(api_bp)
