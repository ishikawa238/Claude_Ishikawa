"""
バックテストモジュール

過去の daily_signals と price_history を使って、
パターン A/B シグナルの期待値を検証する。

- 未来の価格を参照しない（日次ごとの時点情報だけで判定）
- 手数料・スリッページを考慮
- 市場環境モードによる新規買い停止を反映
- 同時保有上限を遵守
"""

from datetime import date, timedelta

from src.config import CONFIG
from src.data_store import fetch_all, fetch_one, get_connection
from src.logger import get_logger

logger = get_logger(__name__)

_CAP = CONFIG["capital"]
_SIG = CONFIG["signal"]
_ATR_MULT = float(_SIG["atr_stop_multiplier"])
_RR_RATIO = float(_SIG["risk_reward_ratio"])
_MAX_HOLDINGS = int(_CAP.get("max_concurrent_holdings", 3))

# デフォルト手数料・スリッページ
_DEFAULT_COMMISSION_PCT = 0.1   # 片道 0.1%
_DEFAULT_SLIPPAGE_PCT = 0.1     # 0.1%


# ============================================================
# メイン: バックテスト実行
# ============================================================

def run_backtest(
    start_date: str,
    end_date: str,
    market: str = "ALL",
    capital: float | None = None,
    commission_pct: float = _DEFAULT_COMMISSION_PCT,
    slippage_pct: float = _DEFAULT_SLIPPAGE_PCT,
) -> list[dict]:
    """
    過去のシグナルデータを使ってバックテストを実行する。

    引数:
        start_date: 開始日（YYYY-MM-DD）
        end_date:   終了日（YYYY-MM-DD）
        market:     "JP" / "US" / "ALL"
        capital:    初期資金（None の場合は CONFIG から取得）
        commission_pct: 片道手数料（%）
        slippage_pct:   スリッページ（%）

    返り値: 完了したトレードのリスト
    """
    if capital is None:
        capital_key = "total_capital_usd" if market == "US" else "total_capital_jpy"
        capital = float(_CAP[capital_key])
    risk_pct = float(_CAP.get("max_risk_per_trade_pct", 2.0)) / 100

    # 期間内の営業日（daily_signals が存在する日）を取得
    dates = _get_signal_dates(start_date, end_date, market)
    if not dates:
        logger.warning("指定期間にシグナルデータがありません。")
        return []

    logger.info(
        f"バックテスト開始: {start_date} ～ {end_date} "
        f"({len(dates)}日間, market={market}, 初期資金={capital:,.0f})"
    )

    open_positions: list[dict] = []  # 保有中のポジション
    closed_trades: list[dict] = []   # 決済済みトレード
    equity = capital

    for i, d in enumerate(dates):
        next_date = dates[i + 1] if i + 1 < len(dates) else None

        # --- 1. 保有ポジションの評価（損切り・利確チェック） ---
        still_open = []
        for pos in open_positions:
            price_row = _get_price(pos["ticker"], d)
            if not price_row:
                still_open.append(pos)
                continue

            high = float(price_row.get("high") or price_row["adj_close"])
            low = float(price_row.get("low") or price_row["adj_close"])
            close = float(price_row["adj_close"])

            # 損切り判定: 安値が損切りラインを下回った
            if low <= pos["stop_loss_price"]:
                exit_price = pos["stop_loss_price"] * (1 - slippage_pct / 100)
                trade = _close_position(pos, exit_price, d, "stop_loss", commission_pct)
                equity += trade["pnl_amount"]
                closed_trades.append(trade)
                continue

            # 利確判定: 高値が利確ラインに到達
            if high >= pos["target_price"]:
                exit_price = pos["target_price"] * (1 - slippage_pct / 100)
                trade = _close_position(pos, exit_price, d, "take_profit", commission_pct)
                equity += trade["pnl_amount"]
                closed_trades.append(trade)
                continue

            still_open.append(pos)

        open_positions = still_open

        # --- 2. 新規エントリー判定 ---
        if next_date is None:
            continue  # 最終日は新規エントリーしない

        # 市場環境チェック
        market_mode = _get_market_mode(d)
        if market_mode in ("caution", "full_caution"):
            continue  # 警戒モードでは新規買いを停止

        # 保有上限チェック
        if len(open_positions) >= _MAX_HOLDINGS:
            continue

        # candidate シグナルを取得
        candidates = _get_candidates(d, market)
        if not candidates:
            continue

        for cand in candidates:
            if len(open_positions) >= _MAX_HOLDINGS:
                break
            # 既に同じ銘柄を保有中なら スキップ
            if any(p["ticker"] == cand["ticker"] for p in open_positions):
                continue

            # 翌日の始値でエントリー
            next_price = _get_price(cand["ticker"], next_date)
            if not next_price or not next_price.get("open"):
                continue

            entry_price = float(next_price["open"]) * (1 + slippage_pct / 100)
            atr14 = float(cand["atr14"]) if cand.get("atr14") else 0
            close_price = float(cand["close"]) if cand.get("close") else entry_price

            # 損切り・利確ライン（シグナル時点の ATR で計算）
            if atr14 > 0:
                stop_loss = entry_price - atr14 * _ATR_MULT
            else:
                stop_loss = entry_price * 0.92

            stop_loss_pct = (entry_price - stop_loss) / entry_price * 100
            if stop_loss_pct < 1.0:
                continue  # 損切り幅が狭すぎる

            target = entry_price * (1 + stop_loss_pct / 100 * _RR_RATIO)

            # ポジションサイズ
            risk_amount = equity * risk_pct
            position_size = min(
                risk_amount / (stop_loss_pct / 100),
                equity / _MAX_HOLDINGS,
                equity,
            )
            if position_size <= 0:
                continue

            # 手数料を考慮
            entry_cost = position_size * commission_pct / 100

            pos = {
                "ticker":          cand["ticker"],
                "market":          cand["market"],
                "pattern":         cand["pattern"],
                "signal_date":     d,
                "entry_date":      next_date,
                "entry_price":     round(entry_price, 4),
                "stop_loss_price": round(stop_loss, 4),
                "target_price":    round(target, 4),
                "position_size":   round(position_size, 2),
                "entry_cost":      round(entry_cost, 2),
            }
            open_positions.append(pos)

    # --- 最終日に未決済のポジションを強制クローズ ---
    if open_positions and dates:
        last_date = dates[-1]
        for pos in open_positions:
            price_row = _get_price(pos["ticker"], last_date)
            if price_row:
                exit_price = float(price_row["adj_close"]) * (1 - slippage_pct / 100)
            else:
                exit_price = pos["entry_price"]  # データなしの場合はエントリー価格
            trade = _close_position(pos, exit_price, last_date, "forced_close", commission_pct)
            equity += trade["pnl_amount"]
            closed_trades.append(trade)

    logger.info(
        f"バックテスト完了: {len(closed_trades)} トレード, "
        f"最終資金 {equity:,.0f}"
    )
    return closed_trades


# ============================================================
# 集計: バックテスト結果のサマリー
# ============================================================

def summarize_results(trades: list[dict], initial_capital: float = 0) -> dict:
    """
    バックテスト結果を集計する。

    返り値: 統計情報の辞書
    """
    if not trades:
        return {
            "total_trades": 0,
            "win_rate": 0.0,
            "avg_profit_pct": 0.0,
            "avg_loss_pct": 0.0,
            "expected_value_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "max_consecutive_losses": 0,
            "total_pnl": 0.0,
            "pattern_stats": {},
        }

    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]

    total = len(trades)
    win_rate = len(wins) / total * 100

    avg_profit_pct = (
        sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0.0
    )
    avg_loss_pct = (
        sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0.0
    )

    # 期待値 = 勝率 × 平均利益 + (1 - 勝率) × 平均損失
    expected_value_pct = (
        (len(wins) / total) * avg_profit_pct
        + (len(losses) / total) * avg_loss_pct
    )

    total_pnl = sum(t["pnl_amount"] for t in trades)

    # 最大連敗数
    max_consecutive_losses = _calc_max_consecutive_losses(trades)

    # 最大ドローダウン
    max_drawdown_pct = _calc_max_drawdown(trades, initial_capital)

    # パターン別統計
    pattern_stats = {}
    for pattern in ("A", "B", "C"):
        p_trades = [t for t in trades if t.get("pattern") == pattern]
        if not p_trades:
            continue
        p_wins = [t for t in p_trades if t["pnl_pct"] > 0]
        p_losses = [t for t in p_trades if t["pnl_pct"] <= 0]
        p_avg_profit = (
            sum(t["pnl_pct"] for t in p_wins) / len(p_wins) if p_wins else 0.0
        )
        p_avg_loss = (
            sum(t["pnl_pct"] for t in p_losses) / len(p_losses) if p_losses else 0.0
        )
        pattern_stats[pattern] = {
            "total": len(p_trades),
            "wins": len(p_wins),
            "win_rate": len(p_wins) / len(p_trades) * 100,
            "avg_profit_pct": round(p_avg_profit, 2),
            "avg_loss_pct": round(p_avg_loss, 2),
        }

    return {
        "total_trades": total,
        "win_rate": round(win_rate, 2),
        "avg_profit_pct": round(avg_profit_pct, 2),
        "avg_loss_pct": round(avg_loss_pct, 2),
        "expected_value_pct": round(expected_value_pct, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "max_consecutive_losses": max_consecutive_losses,
        "total_pnl": round(total_pnl, 2),
        "pattern_stats": pattern_stats,
    }


def format_summary(summary: dict, initial_capital: float = 0) -> str:
    """集計結果を人間が読みやすい文字列に変換する。"""
    lines = [
        "=" * 50,
        "  バックテスト結果サマリー",
        "=" * 50,
    ]

    if summary["total_trades"] == 0:
        lines.append("  トレードなし")
        return "\n".join(lines)

    lines += [
        f"  トレード数:     {summary['total_trades']}",
        f"  勝率:           {summary['win_rate']:.1f}%",
        f"  平均利益:       {summary['avg_profit_pct']:+.2f}%",
        f"  平均損失:       {summary['avg_loss_pct']:+.2f}%",
        f"  期待値:         {summary['expected_value_pct']:+.2f}%",
        f"  最大ドローダウン: {summary['max_drawdown_pct']:.2f}%",
        f"  最大連敗数:     {summary['max_consecutive_losses']}",
        f"  損益合計:       {summary['total_pnl']:+,.0f}",
    ]

    if initial_capital > 0:
        total_return = summary["total_pnl"] / initial_capital * 100
        lines.append(f"  総リターン:     {total_return:+.2f}%")

    if summary["pattern_stats"]:
        lines += ["", "  --- パターン別 ---"]
        for pat, st in sorted(summary["pattern_stats"].items()):
            _BT_PATTERN_NAMES = {"A": "モメンタムブレイクアウト", "B": "押し目買い", "C": "サポートバウンス"}
            name = _BT_PATTERN_NAMES.get(pat, pat)
            lines.append(
                f"  [{pat}] {name}: "
                f"{st['total']}件 勝率{st['win_rate']:.0f}% "
                f"平均利益{st['avg_profit_pct']:+.2f}% "
                f"平均損失{st['avg_loss_pct']:+.2f}%"
            )

    lines.append("=" * 50)
    return "\n".join(lines)


# ============================================================
# ヘルパー（DB アクセス）
# ============================================================

def _get_signal_dates(start: str, end: str, market: str) -> list[str]:
    """期間内で daily_signals が存在する日付リストを昇順で返す。"""
    market_clause = ""
    params: list = [start, end]
    if market != "ALL":
        market_clause = " AND market = ?"
        params.append(market)

    with get_connection() as conn:
        rows = fetch_all(
            conn,
            f"SELECT DISTINCT date FROM daily_signals "
            f"WHERE date >= ? AND date <= ?{market_clause} "
            f"ORDER BY date ASC",
            tuple(params),
        )
    return [r["date"] for r in rows]


def _get_candidates(d: str, market: str) -> list[dict]:
    """指定日の candidate シグナルを final_score 降順で返す。"""
    market_clause = ""
    params: list = [d]
    if market != "ALL":
        market_clause = " AND market = ?"
        params.append(market)

    with get_connection() as conn:
        rows = fetch_all(
            conn,
            f"SELECT * FROM daily_signals "
            f"WHERE date = ? AND label = 'candidate'{market_clause} "
            f"ORDER BY final_score DESC",
            tuple(params),
        )
    return [dict(r) for r in rows]


def _get_market_mode(d: str) -> str:
    """指定日の市場環境モードを返す。データがなければ normal を返す。"""
    with get_connection() as conn:
        row = fetch_one(
            conn,
            "SELECT mode FROM market_environment WHERE date = ?",
            (d,),
        )
    return row["mode"] if row else "normal"


def _get_price(ticker: str, d: str) -> dict | None:
    """指定日の price_history を返す。"""
    with get_connection() as conn:
        row = fetch_one(
            conn,
            "SELECT * FROM price_history WHERE ticker = ? AND date = ?",
            (ticker, d),
        )
    return dict(row) if row else None


# ============================================================
# ヘルパー（計算）
# ============================================================

def _close_position(
    pos: dict,
    exit_price: float,
    exit_date: str,
    reason: str,
    commission_pct: float,
) -> dict:
    """ポジションを決済してトレード記録を返す。"""
    entry_price = pos["entry_price"]
    position_size = pos["position_size"]
    shares = position_size / entry_price

    exit_value = shares * exit_price
    exit_cost = exit_value * commission_pct / 100

    pnl_amount = exit_value - position_size - pos["entry_cost"] - exit_cost
    pnl_pct = (exit_price / entry_price - 1) * 100

    return {
        "ticker":       pos["ticker"],
        "market":       pos["market"],
        "pattern":      pos["pattern"],
        "signal_date":  pos["signal_date"],
        "entry_date":   pos["entry_date"],
        "entry_price":  pos["entry_price"],
        "exit_date":    exit_date,
        "exit_price":   round(exit_price, 4),
        "exit_reason":  reason,
        "position_size": position_size,
        "pnl_amount":   round(pnl_amount, 2),
        "pnl_pct":      round(pnl_pct, 2),
    }


def _calc_max_consecutive_losses(trades: list[dict]) -> int:
    """時系列順で最大連敗数を計算する。"""
    sorted_trades = sorted(trades, key=lambda t: (t["exit_date"], t["ticker"]))
    max_streak = 0
    current = 0
    for t in sorted_trades:
        if t["pnl_pct"] <= 0:
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 0
    return max_streak


def _calc_max_drawdown(trades: list[dict], initial_capital: float) -> float:
    """累積損益ベースの最大ドローダウン（%）を計算する。"""
    if not trades or initial_capital <= 0:
        return 0.0

    sorted_trades = sorted(trades, key=lambda t: (t["exit_date"], t["ticker"]))
    equity = initial_capital
    peak = initial_capital
    max_dd = 0.0

    for t in sorted_trades:
        equity += t["pnl_amount"]
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100
        if dd > max_dd:
            max_dd = dd

    return max_dd
