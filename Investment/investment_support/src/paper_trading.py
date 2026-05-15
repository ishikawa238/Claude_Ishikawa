"""
ペーパートレーディングモジュール

シグナルを実売買せず、仮想エントリー・仮想決済として記録する。
運用開始前の検証期間（1〜3か月）に期待値を確認するために使用する。

主な機能:
- シグナル発生時に仮想エントリーを記録
- 日次バッチで保有中ペーパートレードの損切り・利確を判定
- 統計サマリーを生成
"""

from datetime import date

from src import config as config_module
from src.data_store import fetch_all, fetch_one, get_connection, insert_one
from src.logger import get_logger

logger = get_logger(__name__)


def is_paper_mode_enabled() -> bool:
    """config.json の paper_mode.enabled を確認する。"""
    return _as_bool(config_module.CONFIG.get("paper_mode", {}).get("enabled", False))


# ============================================================
# 仮想エントリー記録
# ============================================================

def record_paper_entries(entry_sets: list[dict]) -> list[dict]:
    """
    エントリーセットから仮想ポジションを paper_trades に記録する。

    引数: save_entry_sets() の返り値（エントリーセットのリスト）
    返り値: 記録したペーパートレードのリスト
    """
    max_holdings = _max_holdings()
    open_count = _count_open_trades()
    recorded = []

    for es in entry_sets:
        if open_count >= max_holdings:
            logger.info(f"ペーパートレード保有上限 ({max_holdings}) に到達。残りをスキップ。")
            break

        # skip 理由がある場合はスキップ
        if es.get("_skip_reason"):
            continue

        ticker = es["ticker"]

        # 既に同銘柄のオープンポジションがあればスキップ
        if _has_open_trade(ticker):
            logger.info(f"ペーパートレード: {ticker} は既にオープン中。スキップ。")
            continue

        row = {
            "signal_date":     es["signal_date"],
            "ticker":          ticker,
            "name":            es.get("name"),
            "market":          es["market"],
            "pattern":         es.get("pattern"),
            "entry_price":     es["entry_price"],
            "stop_loss_price": es["stop_loss_price"],
            "target_price":    es["target_price"],
            "position_size":   es.get("recommended_amount") or 0,
            "status":          "open",
        }

        with get_connection() as conn:
            row["id"] = insert_one(conn, "paper_trades", row)

        recorded.append(row)
        open_count += 1
        logger.info(
            f"ペーパートレード記録: {ticker} "
            f"エントリー:{es['entry_price']} SL:{es['stop_loss_price']} TP:{es['target_price']}"
        )

    logger.info(f"ペーパートレード記録: {len(recorded)} 件")
    return recorded


# ============================================================
# 日次更新（損切り・利確チェック）
# ============================================================

def update_paper_trades(target_date: date) -> dict:
    """
    オープン中のペーパートレードを日次の価格で評価し、
    損切り・利確条件に達したものをクローズする。

    返り値: {"closed": [...], "open": [...]}
    """
    target_str = target_date.strftime("%Y-%m-%d")
    open_trades = _get_open_trades()

    if not open_trades:
        logger.info("ペーパートレード: オープンポジションなし。")
        return {"closed": [], "open": []}

    closed = []
    still_open = []

    for trade in open_trades:
        ticker = trade["ticker"]
        price_row = _get_latest_price(ticker, target_str)

        if not price_row:
            still_open.append(trade)
            continue

        high = float(price_row.get("high") or price_row.get("adj_close") or 0)
        low = float(price_row.get("low") or price_row.get("adj_close") or 0)

        entry_price = float(trade["entry_price"])
        stop_loss = float(trade["stop_loss_price"])
        target = float(trade["target_price"])

        # 損切り判定
        if low <= stop_loss:
            closed_info = _close_trade(
                trade["id"], stop_loss, target_str, "stop_loss",
                entry_price, float(trade.get("position_size") or 0),
            )
            closed.append({**dict(trade), **closed_info})
            continue

        # 利確判定
        if high >= target:
            closed_info = _close_trade(
                trade["id"], target, target_str, "take_profit",
                entry_price, float(trade.get("position_size") or 0),
            )
            closed.append({**dict(trade), **closed_info})
            continue

        still_open.append(trade)

    if closed:
        logger.info(f"ペーパートレード決済: {len(closed)} 件")
    logger.info(f"ペーパートレード残: {len(still_open)} 件オープン中")

    return {"closed": closed, "open": still_open}


# ============================================================
# サマリー生成
# ============================================================

def get_paper_trade_summary() -> dict:
    """
    全ペーパートレードの統計サマリーを返す。
    """
    with get_connection() as conn:
        all_trades = fetch_all(conn, "SELECT * FROM paper_trades ORDER BY signal_date")
        open_trades = fetch_all(
            conn, "SELECT * FROM paper_trades WHERE status = 'open' ORDER BY signal_date"
        )

    closed = [t for t in all_trades if t["status"] == "closed"]

    if not closed:
        return {
            "total": len(all_trades),
            "open": len(open_trades),
            "closed": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "avg_pnl_pct": 0.0,
            "total_pnl": 0.0,
            "open_trades": [dict(t) for t in open_trades],
        }

    wins = [t for t in closed if (t.get("pnl_pct") or 0) > 0]
    losses = [t for t in closed if (t.get("pnl_pct") or 0) <= 0]
    win_rate = len(wins) / len(closed) * 100 if closed else 0.0
    total_pnl = sum(float(t.get("pnl_amount") or 0) for t in closed)
    avg_pnl_pct = (
        sum(float(t.get("pnl_pct") or 0) for t in closed) / len(closed)
        if closed else 0.0
    )

    return {
        "total": len(all_trades),
        "open": len(open_trades),
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "avg_pnl_pct": round(avg_pnl_pct, 2),
        "total_pnl": round(total_pnl, 2),
        "open_trades": [dict(t) for t in open_trades],
    }


def format_summary(summary: dict) -> str:
    """サマリーを表示用文字列に変換する。"""
    lines = [
        "=" * 50,
        "  ペーパートレード サマリー",
        "=" * 50,
        f"  全トレード数:   {summary['total']}",
        f"  オープン中:     {summary['open']}",
        f"  クローズ済み:   {summary['closed']}",
    ]

    if summary["closed"] > 0:
        lines += [
            f"  勝ち:           {summary['wins']}",
            f"  負け:           {summary['losses']}",
            f"  勝率:           {summary['win_rate']:.1f}%",
            f"  平均損益:       {summary['avg_pnl_pct']:+.2f}%",
            f"  損益合計:       {summary['total_pnl']:+,.0f}",
        ]

    if summary["open_trades"]:
        lines += ["", "  --- オープンポジション ---"]
        for t in summary["open_trades"]:
            lines.append(
                f"  {t['ticker']:<12} エントリー:{t['entry_price']} "
                f"SL:{t['stop_loss_price']} TP:{t['target_price']}"
            )

    lines.append("=" * 50)
    return "\n".join(lines)


# ============================================================
# ヘルパー
# ============================================================

def _count_open_trades() -> int:
    with get_connection() as conn:
        row = fetch_one(conn, "SELECT COUNT(*) as cnt FROM paper_trades WHERE status = 'open'")
    return int(row["cnt"]) if row else 0


def _has_open_trade(ticker: str) -> bool:
    with get_connection() as conn:
        row = fetch_one(
            conn,
            "SELECT id FROM paper_trades WHERE ticker = ? AND status = 'open' LIMIT 1",
            (ticker,),
        )
    return row is not None


def _get_open_trades() -> list[dict]:
    with get_connection() as conn:
        return fetch_all(
            conn, "SELECT * FROM paper_trades WHERE status = 'open' ORDER BY signal_date"
        )


def _get_latest_price(ticker: str, date_str: str) -> dict | None:
    """指定日以前の最新価格を返す。"""
    with get_connection() as conn:
        return fetch_one(
            conn,
            "SELECT * FROM price_history "
            "WHERE ticker = ? AND date <= ? AND is_holiday = 0 "
            "ORDER BY date DESC LIMIT 1",
            (ticker, date_str),
        )


def _close_trade(
    trade_id: int,
    exit_price: float,
    exit_date: str,
    reason: str,
    entry_price: float,
    position_size: float,
) -> dict:
    """ペーパートレードをクローズする。"""
    pnl_pct = (exit_price / entry_price - 1) * 100 if entry_price > 0 else 0
    pnl_amount = position_size * pnl_pct / 100 if position_size > 0 else 0

    with get_connection() as conn:
        conn.execute(
            "UPDATE paper_trades SET "
            "  status = 'closed', exit_price = ?, exit_date = ?, "
            "  exit_reason = ?, pnl_pct = ?, pnl_amount = ?, "
            "  updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (
                round(exit_price, 4),
                exit_date,
                reason,
                round(pnl_pct, 2),
                round(pnl_amount, 2),
                trade_id,
            ),
        )
        conn.commit()

    return {
        "exit_price": round(exit_price, 4),
        "exit_date": exit_date,
        "exit_reason": reason,
        "pnl_pct": round(pnl_pct, 2),
        "pnl_amount": round(pnl_amount, 2),
    }


def _max_holdings() -> int:
    return max(int(config_module.CONFIG.get("capital", {}).get("max_concurrent_holdings", 3)), 1)


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)
