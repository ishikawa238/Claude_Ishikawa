"""
ポートフォリオ・シミュレーション（日足・現物の買いのみ。JP / US の単独または合算）。

日本時間の順に処理する。同じ日付では JP の取引日 → US の取引日（settings.MARKETS の順）。
1市場の1営業日の処理順:
  1. 寄付: 前日に決めた決済 → 前日シグナルの新規買い（始値 + スリッページ）
  2. 場中: 損切り（stop_mode=intraday なら安値で判定し、損切り価格と始値の安い方で約定）
  3. 引け: exit_simple の利確・停滞判定（翌営業日の寄付で決済）
  4. 引け後: 運用停止ルール → 市場環境 → 新規シグナル（翌営業日の寄付で約定）

保有枠と運用停止ルールは全市場で共有し、資金は市場ごと（JP は円、US はドル）に持つ。
各日付の処理が終わったら、評価通貨（複数市場なら円）で時価評価する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from src.entry_calculator import calc_recommended_amount
from src.exit_simple import (
    LABEL_STALE_EXIT,
    LABEL_TP_EXTEND_EXIT,
    LABEL_TP_EXTEND_HOLD,
    LABEL_TP_FULL,
    LABEL_TP_PARTIAL,
    evaluate_exit,
    is_exit_simple_enabled,
)
from src.holdings_monitor import _SL_NEAR_PCT

from .settings import MARKETS, BacktestSettings

_FULL_EXIT_LABELS = (LABEL_TP_FULL, LABEL_STALE_EXIT, LABEL_TP_EXTEND_EXIT)

REASON_STOP = "stop_loss"
REASON_PRICE_JUMP = "price_jump"
REASON_END = "end_of_test"

COUNTER_KEYS = (
    "normal_days",
    "caution_days",
    "paused_days",
    "signals",
    "skipped_entry_filter",
    "skipped_held",
    "skipped_slots",
    "skipped_too_small",
    "skipped_no_bar",
    "entries",
)


class Feed(Protocol):
    calendar: list[str]

    def bar(self, ticker: str, k: int) -> dict | None: ...
    def price_jump(self, ticker: str, k: int) -> bool: ...
    def exit_snapshot(self, ticker: str, k: int) -> dict | None: ...
    def market_mode(self, k: int) -> str: ...
    def signals(self, k: int) -> list[dict]: ...
    def entry_set(self, signal: dict, open_count: int) -> dict: ...


@dataclass
class Position:
    position_id: int
    market: str
    ticker: str
    strategy: str | None
    signal_date: str
    entry_date: str
    entry_price: float
    stop_loss_price: float
    shares: int
    initial_shares: int
    entry_commission: float          # 残っている株数分の買い手数料
    last_close: float
    extend_active: bool = False


@dataclass
class EntryOrder:
    ticker: str
    strategy: str | None
    signal_date: str
    stop_loss_pct: float


@dataclass
class ExitOrder:
    shares: int
    reason: str


@dataclass
class SimulationResult:
    settings: BacktestSettings
    trades: list[dict] = field(default_factory=list)
    daily: list[dict] = field(default_factory=list)
    counters: dict[str, dict] = field(default_factory=dict)   # 市場 → 新規買いの判定内訳
    first_pause: dict | None = None


class PortfolioSimulator:
    def __init__(self, settings: BacktestSettings, feeds: dict[str, Feed]):
        missing = [m for m in settings.markets if m not in feeds]
        if missing:
            raise ValueError(f"データが無い市場があります: {missing}")
        self.s = settings
        self.feeds = {m: feeds[m] for m in settings.markets}
        self.cash = {m: settings.capital[m] for m in settings.markets}
        self.positions: dict[str, Position] = {}
        self.pending_entries: dict[str, list[EntryOrder]] = {m: [] for m in settings.markets}
        self.pending_exits: dict[str, ExitOrder] = {}
        self.result = SimulationResult(
            settings=settings,
            counters={m: dict.fromkeys(COUNTER_KEYS, 0) for m in settings.markets},
        )
        self._next_position_id = 1

    # ============================================================
    # 実行
    # ============================================================

    def run(self) -> SimulationResult:
        timeline = self._timeline()
        modes: dict[str, str | None] = {}
        for n, (d, market, k) in enumerate(timeline):
            self._execute_exits(market, k, d)
            self._execute_entries(market, k, d)
            self._evaluate_positions(market, k, d)
            is_last_day = k == len(self.feeds[market].calendar) - 1
            modes[market] = None if is_last_day else self._plan_entries(market, k, d)
            if n + 1 == len(timeline) or timeline[n + 1][0] != d:
                self._mark(d, modes)
                modes = {}
        self._close_all()
        return self.result

    def _timeline(self) -> list[tuple[str, str, int]]:
        """(日付, 市場, その市場の営業日番号) を日本時間の発生順に並べる。"""
        order = {m: i for i, m in enumerate(MARKETS)}
        events = [(d, m, k) for m, feed in self.feeds.items() for k, d in enumerate(feed.calendar)]
        return sorted(events, key=lambda e: (e[0], order[e[1]]))

    # ---------------- 寄付 ----------------

    def _execute_exits(self, market: str, k: int, d: str) -> None:
        for ticker, order in list(self.pending_exits.items()):
            pos = self.positions[ticker]
            if pos.market != market:
                continue
            bar = self.feeds[market].bar(ticker, k)
            if bar is None:
                continue
            del self.pending_exits[ticker]
            self._sell(pos, order.shares, self._sell_price(bar["open"]), d, order.reason)

    def _execute_entries(self, market: str, k: int, d: str) -> None:
        orders, self.pending_entries[market] = self.pending_entries[market], []
        c = self.result.counters[market]
        for order in orders:
            if order.ticker in self.positions:
                c["skipped_held"] += 1
                continue
            if len(self.positions) >= self.s.max_holdings:
                c["skipped_slots"] += 1
                continue
            bar = self.feeds[market].bar(order.ticker, k)
            if bar is None:
                c["skipped_no_bar"] += 1
                continue
            price = bar["open"] * (1 + self.s.slippage_pct / 100)
            shares = self._entry_shares(market, price, order.stop_loss_pct)
            if shares <= 0:
                c["skipped_too_small"] += 1
                continue
            cost = shares * price
            commission = cost * self.s.commission_pct / 100
            self.cash[market] -= cost + commission
            self.positions[order.ticker] = Position(
                position_id=self._next_position_id,
                market=market,
                ticker=order.ticker,
                strategy=order.strategy,
                signal_date=order.signal_date,
                entry_date=d,
                entry_price=price,
                stop_loss_price=price * (1 - order.stop_loss_pct / 100),
                shares=shares,
                initial_shares=shares,
                entry_commission=commission,
                last_close=price,
            )
            self._next_position_id += 1
            c["entries"] += 1

    def _entry_shares(self, market: str, price: float, stop_loss_pct: float) -> int:
        """推奨投入額（本番と同じ式）を、その市場の手元資金と売買単位で切り下げた株数。"""
        basis = self.s.capital[market] if self.s.sizing == "fixed" else self._market_equity(market)
        amount = calc_recommended_amount(basis, stop_loss_pct, self.s.risk_pct, self.s.max_holdings)
        if not amount or amount <= 0:
            return 0
        amount = min(amount, self.cash[market] / (1 + self.s.commission_pct / 100))
        lot = self.s.lot_size
        return int(amount // (price * lot)) * lot

    # ---------------- 場中・引け ----------------

    def _evaluate_positions(self, market: str, k: int, d: str) -> None:
        feed = self.feeds[market]
        for ticker, pos in list(self.positions.items()):
            if pos.market != market or ticker in self.pending_exits:
                continue
            bar = feed.bar(ticker, k)
            if bar is None:
                continue
            if feed.price_jump(ticker, k):
                self._sell(pos, pos.shares, pos.last_close, d, REASON_PRICE_JUMP)
                continue
            if self.s.stop_mode == "intraday" and bar["low"] <= pos.stop_loss_price:
                fill = self._sell_price(min(bar["open"], pos.stop_loss_price))
                self._sell(pos, pos.shares, fill, d, REASON_STOP)
                continue
            pos.last_close = bar["close"]
            if self.s.stop_mode == "close" and bar["close"] <= pos.stop_loss_price:
                self.pending_exits[ticker] = ExitOrder(pos.shares, REASON_STOP)
                continue
            self._evaluate_exit_simple(pos, feed, k, d, bar["close"])

    def _evaluate_exit_simple(self, pos: Position, feed: Feed, k: int, d: str, close: float) -> None:
        """holdings_monitor.evaluate_holdings と同じ条件で exit_simple を呼ぶ。"""
        if not is_exit_simple_enabled():
            return
        if (close - pos.stop_loss_price) / close * 100 <= _SL_NEAR_PCT:
            return
        snap = feed.exit_snapshot(pos.ticker, k)
        result = evaluate_exit(
            pos.ticker,
            pos.entry_price,
            close,
            (close - pos.entry_price) / pos.entry_price * 100,
            pos.shares,
            (date.fromisoformat(d) - date.fromisoformat(pos.entry_date)).days,
            snap,
            resistance_level=snap.get("resistance_level") if snap else None,
            stop_loss_price=pos.stop_loss_price,
            persist_state=False,
            state={"extend_active": pos.extend_active},
        )
        if not result:
            return
        label = result["label"]
        if label in _FULL_EXIT_LABELS:
            self.pending_exits[pos.ticker] = ExitOrder(pos.shares, label)
        elif label == LABEL_TP_PARTIAL:
            qty = self._lot_round(int(result["sell_qty"]), pos.shares)
            self.pending_exits[pos.ticker] = ExitOrder(qty, label)
            pos.extend_active = True
        elif label == LABEL_TP_EXTEND_HOLD:
            pos.extend_active = True

    def _lot_round(self, qty: int, held: int) -> int:
        lot = self.s.lot_size
        qty = max(qty // lot * lot, lot)
        return held if held - qty < lot else qty

    # ---------------- 引け後 ----------------

    def _plan_entries(self, market: str, k: int, d: str) -> str:
        """運用停止ルールと市場環境を確認し、翌営業日に買う候補を決める。"""
        c = self.result.counters[market]
        feed = self.feeds[market]
        mode = feed.market_mode(k)
        if mode != "normal":
            c["caution_days"] += 1
            return mode
        c["normal_days"] += 1
        pause = self._pause_reason(d) if self.s.risk_control else None
        if pause:
            c["paused_days"] += 1
            if self.result.first_pause is None:
                self.result.first_pause = {"date": d, "market": market, "reason": pause}
            return "paused"

        signals = feed.signals(k)
        c["signals"] += len(signals)
        committed = self._committed_slots(market)
        planned: list[EntryOrder] = []
        for sig in signals:
            if sig["ticker"] in self.positions:
                c["skipped_held"] += 1
                continue
            entry_set = feed.entry_set(sig, committed + len(planned))
            if not entry_set:
                c["skipped_entry_filter"] += 1
                continue
            if committed + len(planned) >= self.s.max_holdings:
                c["skipped_slots"] += 1
                continue
            planned.append(EntryOrder(
                ticker=sig["ticker"],
                strategy=sig.get("strategy"),
                signal_date=d,
                stop_loss_pct=float(entry_set["stop_loss_pct"]),
            ))
        self.pending_entries[market] = planned
        return mode

    def _committed_slots(self, market: str) -> int:
        """使用中の保有枠 = 保有数 - 全量決済待ち + 他市場の約定待ち注文。"""
        full_exits = sum(
            1 for t, o in self.pending_exits.items() if o.shares >= self.positions[t].shares
        )
        others = sum(len(o) for m, o in self.pending_entries.items() if m != market)
        return len(self.positions) - full_exits + others

    def _pause_reason(self, d: str) -> str | None:
        """risk_control.get_trade_pause_reason と同じ条件をバックテスト内の決済で判定する。"""
        trades = self.result.trades
        if self.s.monthly_loss_stop_pct > 0:
            month_start = d[:8] + "01"
            monthly = sum(
                t["pnl_amount"] * self.s.fx(t["market"])
                for t in trades if month_start <= t["exit_date"] <= d
            )
            if monthly <= -(self.s.initial_capital * self.s.monthly_loss_stop_pct / 100):
                return "monthly_loss"
        if self.s.max_consecutive_losses > 0:
            streak = 0
            for t in reversed(trades):
                if t["pnl_pct"] > 0:
                    break
                streak += 1
            if streak >= self.s.max_consecutive_losses:
                return "consecutive_losses"
        return None

    # ---------------- 約定・評価 ----------------

    def _sell_price(self, price: float) -> float:
        return price * (1 - self.s.slippage_pct / 100)

    def _sell(self, pos: Position, shares: int, price: float, d: str, reason: str) -> None:
        shares = min(shares, pos.shares)
        proceeds = shares * price
        commission = proceeds * self.s.commission_pct / 100
        entry_commission = pos.entry_commission * shares / pos.shares
        pnl = (price - pos.entry_price) * shares - entry_commission - commission
        risk_per_share = pos.entry_price - pos.stop_loss_price

        self.cash[pos.market] += proceeds - commission
        pos.entry_commission -= entry_commission
        pos.shares -= shares
        self.result.trades.append({
            "position_id": pos.position_id,
            "market": pos.market,
            "ticker": pos.ticker,
            "strategy": pos.strategy,
            "signal_date": pos.signal_date,
            "entry_date": pos.entry_date,
            "entry_price": pos.entry_price,
            "stop_loss_price": pos.stop_loss_price,
            "initial_shares": pos.initial_shares,
            "exit_date": d,
            "exit_price": price,
            "shares": shares,
            "exit_reason": reason,
            "is_partial": pos.shares > 0,
            "pnl_amount": pnl,
            "pnl_pct": pnl / (pos.entry_price * shares) * 100,
            "r_multiple": (price - pos.entry_price) / risk_per_share if risk_per_share > 0 else None,
            "holding_days": (date.fromisoformat(d) - date.fromisoformat(pos.entry_date)).days,
        })
        if pos.shares <= 0:
            del self.positions[pos.ticker]

    def _market_equity(self, market: str) -> float:
        """その市場の通貨での評価額（現金 + 保有株の直近終値）。"""
        held = sum(p.shares * p.last_close for p in self.positions.values() if p.market == market)
        return self.cash[market] + held

    def _equity(self) -> float:
        return sum(self._market_equity(m) * self.s.fx(m) for m in self.feeds)

    def _mark(self, d: str, modes: dict[str, str | None]) -> None:
        row = {"date": d, "equity": self._equity(), "positions": len(self.positions)}
        for m in self.feeds:
            row[f"cash_{m}"] = self.cash[m]
            row[f"mode_{m}"] = modes.get(m)
        self.result.daily.append(row)

    def _close_all(self) -> None:
        """期間の最終日に残ったポジションを終値で手仕舞う（評価額を確定させるため）。"""
        for pos in list(self.positions.values()):
            last_day = self.feeds[pos.market].calendar[-1]
            self._sell(pos, pos.shares, self._sell_price(pos.last_close), last_day, REASON_END)
        self.pending_entries = {m: [] for m in self.feeds}
        self.pending_exits = {}
        if self.result.daily:
            last = self.result.daily[-1]
            last["equity"] = self._equity()
            last["positions"] = 0
            for m in self.feeds:
                last[f"cash_{m}"] = self.cash[m]
