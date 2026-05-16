"""
運用停止ルールの判定モジュール。

月間損失や連続損失が設定値に達した場合、新規エントリーを止める。
"""

from datetime import date, datetime

from src.config import CONFIG
from src.data_store import fetch_all, fetch_one, get_connection
from src.logger import get_logger

logger = get_logger(__name__)


def get_trade_pause_reason(target_date: date | str | None = None) -> str | None:
    """新規買いを止めるべき場合は理由を返す。"""
    cfg = CONFIG.get("risk_control", {})
    monthly_loss_stop_pct = _to_float(cfg.get("monthly_loss_stop_pct", 0), 0.0)
    max_losses = _to_int(cfg.get("max_consecutive_losses_before_pause", 0), 0)
    target = _date_str(target_date)

    if monthly_loss_stop_pct > 0:
        monthly_pnl = _monthly_realized_pnl(target)
        capital = _total_capital_jpy()
        if capital > 0 and monthly_pnl <= -(capital * monthly_loss_stop_pct / 100):
            return (
                f"月間損失が停止ラインに到達 "
                f"({monthly_pnl:,.0f}円 / 許容 {monthly_loss_stop_pct:.1f}%)"
            )

    if max_losses > 0:
        streak = _consecutive_losses()
        if streak >= max_losses:
            return f"連続損失が停止ラインに到達 ({streak}回連続)"

    return None


def _monthly_realized_pnl(target: str) -> float:
    month_start = target[:8] + "01"
    try:
        with get_connection() as conn:
            row = fetch_one(
                conn,
                "SELECT SUM(pnl_amount) AS pnl FROM trade_result_log "
                "WHERE actual_exit_date >= ? AND actual_exit_date <= ?",
                (month_start, target),
            )
    except Exception as e:
        logger.warning(f"月間損失停止ルールをスキップしました: {e}")
        return 0.0
    return _to_float(row["pnl"], 0.0) if row else 0.0


def _consecutive_losses() -> int:
    try:
        with get_connection() as conn:
            rows = fetch_all(
                conn,
                "SELECT pnl_pct FROM trade_result_log "
                "WHERE pnl_pct IS NOT NULL "
                "ORDER BY actual_exit_date DESC, id DESC LIMIT 20",
            )
    except Exception as e:
        logger.warning(f"連続損失停止ルールをスキップしました: {e}")
        return 0

    streak = 0
    for row in rows:
        pnl_pct = _to_float(row["pnl_pct"], 0.0)
        if pnl_pct <= 0:
            streak += 1
        else:
            break
    return streak


def _total_capital_jpy() -> float:
    cap = CONFIG.get("capital", {})
    return (
        _to_float(cap.get("total_capital_jpy", 0), 0.0)
        + _to_float(cap.get("total_capital_usd", 0), 0.0)
        * _to_float(cap.get("usdjpy_fallback", 150.0), 150.0)
    )


def _date_str(value: date | str | None) -> str:
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, str) and value:
        return value[:10]
    return datetime.now().strftime("%Y-%m-%d")


def _to_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
