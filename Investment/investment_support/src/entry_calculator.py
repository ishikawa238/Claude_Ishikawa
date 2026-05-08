"""
エントリーセット計算モジュール

各 candidate シグナルに対して以下を計算する:
- エントリー価格（翌日寄付目安 = 当日終値）
- 損切りライン（エントリー価格 - ATR × 2）
- 損切り幅（%）
- 利確目安（リスクリワード 2:1）
- 推奨投入額（総資金 × 2% / 損切り幅）
- 投入額の上限制御（総資金・同時保有上限）
- 警告フラグ（損切り幅が狭すぎ / 広すぎ）
"""

import json
from datetime import date, datetime

from src.config import CONFIG
from src.data_store import fetch_one, get_connection, insert_one
from src.logger import get_logger

logger = get_logger(__name__)

_SIG = CONFIG["signal"]
_CAP = CONFIG["capital"]

_ATR_MULT       = float(_SIG["atr_stop_multiplier"])
_RR_RATIO       = float(_SIG["risk_reward_ratio"])
_WARN_MIN_PCT   = float(_SIG["stop_loss_warning_min_pct"])
_WARN_MAX_PCT   = float(_SIG["stop_loss_warning_max_pct"])
_RISK_PCT       = float(_CAP["max_risk_per_trade_pct"]) / 100  # 0.02
_CAPITAL_JPY    = float(_CAP["total_capital_jpy"])
_CAPITAL_USD    = float(_CAP["total_capital_usd"])
_MAX_HOLDINGS   = int(_CAP.get("max_concurrent_holdings", 3))
_EARNINGS_WARN  = int(CONFIG.get("jquants", {}).get("earnings_alert_days_warning", 7))


# ============================================================
# エントリーセット計算
# ============================================================

def calc_entry_set(signal: dict, open_holdings_count: int = 0) -> dict:
    """
    シグナル1件のエントリーセットを計算して返す。

    引数: daily_signals の1行（候補銘柄の情報を含む辞書）
    返り値: trade_signal_log への INSERT 用辞書
    """
    close   = float(signal.get("close") or 0)
    atr14   = float(signal.get("atr14") or 0)
    market  = signal["market"]
    ticker  = signal["ticker"]
    pattern = signal.get("pattern") or "A"

    if close <= 0:
        logger.warning(f"終値が0のためエントリーセット計算スキップ: {ticker}")
        return {}

    entry_price = close  # 翌日寄付目安

    # 損切りライン: ATR × 2 を下回った水準
    stop_loss_price = entry_price - atr14 * _ATR_MULT if atr14 > 0 else entry_price * (1 - 0.08)
    stop_loss_pct   = (entry_price - stop_loss_price) / entry_price * 100

    # 損切り幅が1%未満 → エントリー候補から除外
    if stop_loss_pct < _WARN_MIN_PCT:
        logger.info(f"損切り幅が狭すぎるため除外: {ticker}（{stop_loss_pct:.2f}%）")
        return {}

    # 利確目安: リスクリワード 2:1
    target_price = entry_price * (1 + stop_loss_pct / 100 * _RR_RATIO)

    # 推奨投入額
    capital = _CAPITAL_JPY if market == "JP" else _CAPITAL_USD
    if stop_loss_pct > 0:
        recommended_amount = capital * _RISK_PCT / (stop_loss_pct / 100)
    else:
        recommended_amount = None

    # --- 投入額の上限制御 ---
    if recommended_amount is not None:
        # (1) 総資金を超えない
        recommended_amount = min(recommended_amount, capital)
        # (2) 既存保有数を考慮した1ポジションあたりの上限
        effective_slots = max(_MAX_HOLDINGS, open_holdings_count + 1)
        max_per_position = capital / effective_slots
        recommended_amount = min(recommended_amount, max_per_position)

    # --- 決算近接チェック ---
    warn_earnings = _check_earnings_near(ticker, signal["date"])

    # 充足条件リスト（通知の理由文生成に使用）
    conditions_met = _build_conditions_met(signal, pattern)

    result = {
        "signal_date":        signal["date"],
        "ticker":             ticker,
        "name":               signal.get("name"),
        "market":             market,
        "pattern":            pattern,
        "entry_price":        round(entry_price, 2),
        "stop_loss_price":    round(stop_loss_price, 2),
        "stop_loss_pct":      round(stop_loss_pct, 2),
        "target_price":       round(target_price, 2),
        "recommended_amount": round(recommended_amount, 0) if recommended_amount else None,
        "market_mode":        signal.get("market_mode", "normal"),
        "conditions_met":     json.dumps(conditions_met, ensure_ascii=False),
        # 警告フラグ（通知側で使用・DB保存対象外）
        "_warn_narrow":  False,  # 1%未満は上流で除外済み
        "_warn_wide":    stop_loss_pct > _WARN_MAX_PCT,
        "_warn_earnings": warn_earnings,
        "_warn_at_capacity": open_holdings_count >= _MAX_HOLDINGS,
        "_remaining_slots": max(_MAX_HOLDINGS - open_holdings_count, 0),
        # 通知の根拠数値（DB保存対象外・notifier で使用）
        "_rsi14":    float(signal["rsi14"])    if signal.get("rsi14")    else None,
        "_vol_ratio": float(signal["vol_ratio"]) if signal.get("vol_ratio") else None,
        "_rs_rank":   signal.get("rs_rank"),
        # スコアリング情報（DB保存対象外・notifier で使用）
        "_final_score":      signal.get("final_score"),
        "_recommend_rank":   signal.get("recommend_rank"),
        "_recommend_reason": signal.get("recommend_reason"),
        # サブスコア内訳（DB保存対象外・notifier で使用）
        "_trend_score":      signal.get("trend_score"),
        "_pattern_a_score":  signal.get("pattern_a_score"),
        "_pattern_b_score":  signal.get("pattern_b_score"),
        "_liquidity_score":  signal.get("liquidity_score"),
        # テクニカル指標（DB保存対象外・notifier で使用）
        "_high_52w": signal.get("high_52w"),
        "_close":    signal.get("close"),
        "_ma5":      signal.get("ma5"),
        "_ma25":     signal.get("ma25"),
        "_ma75":     signal.get("ma75"),
        "_ma200":    signal.get("ma200"),
    }
    return result


def save_entry_sets(candidates: list[dict], market_mode: str) -> list[dict]:
    """
    candidates リストのエントリーセットを計算して trade_signal_log に保存する。

    返り値: 保存したエントリーセットのリスト（通知生成に使用）
    """
    open_count = _count_open_holdings()
    saved = []
    for sig in candidates:
        sig["market_mode"] = market_mode
        entry_set = calc_entry_set(sig, open_holdings_count=open_count)
        if not entry_set:
            continue

        # 警告フラグは保存しない（_ プレフィックスで除外）
        db_row = {k: v for k, v in entry_set.items() if not k.startswith("_")}
        with get_connection() as conn:
            entry_set["id"] = insert_one(conn, "trade_signal_log", db_row)

        saved.append(entry_set)
        _log_entry_set(entry_set)

    logger.info(f"エントリーセット保存: {len(saved)} 件")
    return saved


# ============================================================
# ヘルパー
# ============================================================

def _count_open_holdings() -> int:
    """保有中（未決済）のポジション数を返す。"""
    with get_connection() as conn:
        row = fetch_one(conn, "SELECT COUNT(*) AS cnt FROM holdings WHERE is_closed = 0")
    return row["cnt"] if row else 0


def _build_conditions_met(signal: dict, pattern: str) -> list[str]:
    """充足した条件の説明文リストを返す（Slack通知の理由欄に使用）。"""
    labels = {
        "A": {
            "cond_a_ma200_rising":  "MA200上昇中",
            "cond_a_ma_aligned":    "短期〜長期MA整列",
            "cond_a_52w_high_near": "52週高値更新圏",
            "cond_a_volume_surge":  f"出来高急増（20日比{signal.get('vol_ratio', 0):.1f}倍）" if signal.get("vol_ratio") else "出来高急増",
            "cond_a_rsi_ok":        f"RSI適正（{signal.get('rsi14', 0):.0f}）" if signal.get("rsi14") else "RSI適正",
            "cond_a_above_ma25":    "終値がMA25上方",
        },
        "B": {
            "cond_b_ma200_rising":     "MA200上昇中",
            "cond_b_ma_aligned":       "中期〜長期MA整列",
            "cond_b_pullback_touched": f"MA25へのタッチ確認（直近{_SIG['pattern_b_lookback_days']}日）",
            "cond_b_bounced":          "MA25からの反発確認",
            "cond_b_ma25_rising":      "MA25上昇中",
            "cond_b_rsi_ok":           f"RSI適正（{signal.get('rsi14', 0):.0f}）" if signal.get("rsi14") else "RSI適正",
        },
    }
    met = []
    for key, desc in labels.get(pattern, {}).items():
        if signal.get(key) == 1:
            met.append(f"✅ {desc}")
        elif signal.get(key) == 0:
            met.append(f"❌ {desc}")
    return met


def _check_earnings_near(ticker: str, date_str: str) -> str | None:
    """決算発表が _EARNINGS_WARN 日以内に迫っている場合、警告文を返す。"""
    try:
        with get_connection() as conn:
            row = fetch_one(
                conn,
                "SELECT announcement_date FROM earnings_schedule "
                "WHERE ticker=? AND announcement_date>=? "
                "ORDER BY announcement_date ASC LIMIT 1",
                (ticker, date_str),
            )
        if not row:
            return None
        target_dt = datetime.strptime(date_str, "%Y-%m-%d").date()
        ann_dt = datetime.strptime(row["announcement_date"], "%Y-%m-%d").date()
        days = (ann_dt - target_dt).days
        if days <= _EARNINGS_WARN:
            return f"決算発表まで{days}日（{row['announcement_date']}）"
    except Exception:
        pass
    return None


def _log_entry_set(es: dict) -> None:
    """エントリーセットの内容をログに出力する。"""
    warn = ""
    if es.get("_warn_narrow"):
        warn = " ⚠️ 損切り幅が狭すぎます"
    elif es.get("_warn_wide"):
        warn = " ⚠️ 損切り幅が広すぎます（高ボラティリティ）"
    logger.info(
        f"  [{es['pattern']}] {es['ticker']} {es['market']} | "
        f"エントリー:{es['entry_price']} | "
        f"損切り:{es['stop_loss_price']}（{es['stop_loss_pct']:.1f}%）| "
        f"利確:{es['target_price']}{warn}"
    )
