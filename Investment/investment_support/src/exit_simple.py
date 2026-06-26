"""
シンプル出口ルール（時間減衰利確 + トレンド延長）。

- 保有日数に応じて利確目標%を段階的に下げる（20→15→10→5%）
- 目標到達時: 延長条件なし→全売 / あり→2株以上は半分売却、1株は温存
- 延長中はトレンド条件が崩れたら残り全売推奨
- 長期停滞株は時間切れで売却検討
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from src.config import get_exit_simple_config
from src.data_store import fetch_one, get_connection
from src.logger import get_logger

logger = get_logger(__name__)

# holdings_monitor / notifier 用ラベル
LABEL_TP_FULL = "tp_full"
LABEL_TP_PARTIAL = "tp_partial"
LABEL_TP_EXTEND_HOLD = "tp_extend_hold"
LABEL_TP_EXTEND_EXIT = "tp_extend_exit"
LABEL_STALE_WATCH = "stale_watch"
LABEL_STALE_EXIT = "stale_exit"

_PRIORITY = {
    LABEL_STALE_EXIT: 90,
    LABEL_TP_EXTEND_EXIT: 85,
    LABEL_TP_FULL: 80,
    LABEL_TP_PARTIAL: 75,
    LABEL_TP_EXTEND_HOLD: 70,
    LABEL_STALE_WATCH: 65,
}


def is_exit_simple_enabled() -> bool:
    cfg = get_exit_simple_config()
    return bool(cfg.get("enabled", True))


def effective_target_pct(holding_days: Optional[int], cfg: Optional[dict] = None) -> float:
    """保有日数から有効な利確目標%を返す。"""
    cfg = cfg or get_exit_simple_config()
    initial = float(cfg.get("initial_target_pct", 20.0))
    decay = float(cfg.get("target_decay_pct", 5.0))
    every = int(cfg.get("target_decay_every_days", 30))
    floor = float(cfg.get("target_floor_pct", 5.0))
    days = max(int(holding_days or 0), 0)
    periods = days // every if every > 0 else 0
    return max(initial - periods * decay, floor)


def target_price(entry_price: float, target_pct: float) -> float:
    return round(entry_price * (1 + target_pct / 100), 2)


def hold_conditions_met(
    snap: Optional[dict],
    current_price: float,
    cfg: Optional[dict] = None,
) -> tuple[bool, list[str]]:
    """延長保有に必要な3条件を評価する。"""
    cfg = cfg or get_exit_simple_config()
    rsi_max = float(cfg.get("rsi_max_for_hold", 70.0))
    failures: list[str] = []

    if not snap:
        return False, ["テクニカルデータなし"]

    ema25 = snap.get("ema25")
    ema75 = snap.get("ema75")
    ma25 = snap.get("ma25")
    rsi = snap.get("rsi14")

    if ema25 is None or ema75 is None or float(ema25) <= float(ema75):
        failures.append("EMA25≯EMA75")
    if ma25 is None or current_price < float(ma25):
        dist = ((float(ma25) - current_price) / float(ma25) * 100) if ma25 else 0
        failures.append(f"終値がMA25を{dist:.1f}%下回り")
    if rsi is not None and float(rsi) > rsi_max:
        failures.append(f"RSI過熱（{float(rsi):.0f}>{rsi_max:g}）")

    return len(failures) == 0, failures


def evaluate_exit(
    ticker: str,
    entry_price: float,
    current_price: float,
    pnl_pct: float,
    quantity: Optional[float],
    holding_days: Optional[int],
    snap: Optional[dict],
    persist_state: bool = True,
) -> Optional[dict]:
    """
    シンプル出口ルールを評価する。

    返り値（発火時）:
        label, reasons, sell_qty, effective_target_pct, effective_target_price,
        extend_active, hold_ok, priority
    """
    if not is_exit_simple_enabled():
        return None

    cfg = get_exit_simple_config()
    qty = max(int(float(quantity or 1)), 1)
    state = get_state(ticker) if persist_state else {}
    extend_active = bool(state.get("extend_active"))
    eff_pct = effective_target_pct(holding_days, cfg)
    eff_price = target_price(entry_price, eff_pct)
    hold_ok, hold_failures = hold_conditions_met(snap, current_price, cfg)
    ratio = float(cfg.get("partial_sell_ratio", 0.5))

    stale_soft = int(cfg.get("stale_days_soft", 90))
    stale_hard = int(cfg.get("stale_days_hard", 120))
    stale_min = float(cfg.get("stale_min_pnl_pct", 5.0))
    stale_hard_min = float(cfg.get("stale_hard_min_pnl_pct", 10.0))
    days = holding_days if holding_days is not None else -1

    base = {
        "effective_target_pct": eff_pct,
        "effective_target_price": eff_price,
        "extend_active": extend_active,
        "hold_ok": hold_ok,
        "sell_qty": 0,
        "priority": 0,
    }

    # --- 長期停滞（延長中でも優先） ---
    if days >= stale_hard:
        if pnl_pct < stale_hard_min:
            reasons = [
                f"保有{days}日超・含み益{pnl_pct:+.1f}%（目標{stale_hard_min:g}%未満）",
                "長期停滞のため全量手仕舞いを推奨",
            ]
            if persist_state:
                clear_state(ticker)
            return _result(LABEL_STALE_EXIT, reasons, qty, base)
        if pnl_pct < 0:
            reasons = [
                f"保有{days}日超・含み損{pnl_pct:+.1f}%",
                "長期保有の損切り検討",
            ]
            return _result(LABEL_STALE_EXIT, reasons, qty, base)

    if days >= stale_soft and pnl_pct < stale_min:
        reasons = [
            f"保有{days}日超・含み益{pnl_pct:+.1f}%（目標{stale_min:g}%未満）",
            "パフォーマンス不振のため売却を検討",
        ]
        return _result(LABEL_STALE_WATCH, reasons, 0, base)

    # --- 延長保有中 ---
    if extend_active:
        if not hold_ok:
            reasons = [
                "延長保有条件が崩れました",
                " / ".join(hold_failures),
                "残りポジションの手仕舞いを推奨",
            ]
            if persist_state:
                clear_state(ticker)
            return _result(LABEL_TP_EXTEND_EXIT, reasons, qty, base)
        return _result(
            "hold",
            [f"延長保有中（利確ライン{eff_pct:g}%到達済み・トレンド継続）"],
            0,
            {**base, "extend_active": True, "priority": 0},
        )

    # --- 利確ライン到達 ---
    if pnl_pct >= eff_pct:
        if hold_ok:
            if qty >= 2:
                sell_qty = max(int(qty * ratio), 1)
                sell_qty = min(sell_qty, qty - 1) if qty > 1 else sell_qty
                reasons = [
                    f"利確ライン到達（+{eff_pct:g}% / 目標価格 {eff_price:,.2f}）",
                    "トレンド継続のため半分売却・残りは延長保有",
                    "（EMA25>EMA75 / MA25上 / RSI適正）",
                ]
                if persist_state:
                    save_state(ticker, extend_active=True, partial_taken=True, last_target_pct=eff_pct)
                return _result(LABEL_TP_PARTIAL, reasons, sell_qty, base)
            reasons = [
                f"利確ライン到達（+{eff_pct:g}% / 目標価格 {eff_price:,.2f}）",
                "トレンド継続のため1株は温存（半分売却不可）",
            ]
            if persist_state:
                save_state(ticker, extend_active=True, partial_taken=False, last_target_pct=eff_pct)
            return _result(LABEL_TP_EXTEND_HOLD, reasons, 0, base)

        reasons = [
            f"利確ライン到達（+{eff_pct:g}% / 目標価格 {eff_price:,.2f}）",
            "延長条件未達のため全量売却を推奨",
        ]
        if hold_failures:
            reasons.append("未達: " + " / ".join(hold_failures))
        if persist_state:
            clear_state(ticker)
        return _result(LABEL_TP_FULL, reasons, qty, base)

    return None


def get_state(ticker: str) -> dict:
    with get_connection() as conn:
        row = fetch_one(
            conn,
            "SELECT ticker, extend_active, partial_taken, last_target_pct "
            "FROM exit_simple_state WHERE ticker = ?",
            (ticker,),
        )
    return dict(row) if row else {}


def save_state(
    ticker: str,
    *,
    extend_active: bool,
    partial_taken: bool,
    last_target_pct: Optional[float] = None,
) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO exit_simple_state
                (ticker, extend_active, partial_taken, last_target_pct, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                extend_active = excluded.extend_active,
                partial_taken = excluded.partial_taken,
                last_target_pct = excluded.last_target_pct,
                updated_at = excluded.updated_at
            """,
            (
                ticker,
                1 if extend_active else 0,
                1 if partial_taken else 0,
                last_target_pct,
                now,
            ),
        )


def clear_state(ticker: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM exit_simple_state WHERE ticker = ?", (ticker,))


def cleanup_closed_holdings() -> int:
    """決済済み銘柄の状態行を削除する。"""
    with get_connection() as conn:
        cur = conn.execute(
            """
            DELETE FROM exit_simple_state
            WHERE ticker NOT IN (
                SELECT ticker FROM holdings WHERE is_closed = 0
            )
            """
        )
        return cur.rowcount


def _result(label: str, reasons: list[str], sell_qty: int, base: dict) -> dict:
    priority = _PRIORITY.get(label, base.get("priority", 0))
    if label == "hold":
        priority = 0
    return {
        **base,
        "label": label,
        "reasons": reasons,
        "sell_qty": sell_qty,
        "priority": priority,
    }
