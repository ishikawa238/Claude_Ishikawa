"""
日次シグナルの再現と、シミュレータへのデータ供給。

市場環境・RS スコア・流動性フィルタはバッチと同じ条件を MarketData 上で再現し、
候補の判定と並び順は本番の signal_detector / entry_calculator の関数をそのまま呼ぶ。
"""

from __future__ import annotations

import math
from bisect import bisect_right
from datetime import date

import numpy as np

from src.config import get_rs_score_config, get_swing_config
from src.entry_calculator import calc_entry_set
from src.market_filter import _calc_mode, get_market_filter_config
from src.rs_score import apply_rs_filter, percentile_scores
from src.signal_detector import (
    _enabled_strategies,
    _evaluate_strategy,
    select_notify_candidates,
)

from .market_data import IndexSeries, MarketData, TickerData

_MARKET_INDEX = {"JP": "N225", "US": "SP500"}

_TK_FIELDS = (
    "ema25", "ema75", "rsi14", "atr14", "ma25",
    "macd_diff", "macd_dea", "macd_hist",
    "macd_diff_prev", "macd_dea_prev", "macd_hist_prev",
    "vol_20d_avg", "high_60d_prior",
)


class MarketFeed:
    """シミュレータが日ごとに参照する価格・指標・シグナルの窓口。"""

    def __init__(self, data: MarketData):
        self.data = data
        self.market = data.market
        self.calendar = data.calendar
        self._strategies = _enabled_strategies(data.market)
        self._rsi_lookback = max(int(get_swing_config().get("rsi_lookback_days", 5)), 1)
        self._signal_cache: dict[int, list[dict]] = {}

    # ---------------- 価格・指標 ----------------

    def bar(self, ticker: str, k: int) -> dict | None:
        td, i = self._locate(ticker, k)
        if td is None:
            return None
        return {
            "open": float(td.opens[i]),
            "high": float(td.highs[i]),
            "low": float(td.lows[i]),
            "close": float(td.closes[i]),
        }

    def price_jump(self, ticker: str, k: int) -> bool:
        td, i = self._locate(ticker, k)
        return bool(td is not None and td.jump[i])

    def exit_snapshot(self, ticker: str, k: int) -> dict | None:
        """exit_simple.evaluate_exit に渡すテクニカル（holdings_monitor の snap 相当）。"""
        td, i = self._locate(ticker, k)
        if td is None:
            return None
        snap = {f: _value(td.ind[f][i]) for f in ("ema25", "ema75", "ma25", "rsi14")}
        snap["resistance_level"] = td.support_resistance(i)[1]
        return snap

    # ---------------- 市場環境 ----------------

    def market_mode(self, k: int) -> str:
        """market_filter.judge_market_environment + mode_for_market と同じ判定。"""
        d = self.calendar[k]
        max_stale = int(get_market_filter_config()["index_max_stale_days"])
        jp = self._index_flag("N225", d, max_stale)
        us = self._index_flag("SP500", d, max_stale)
        if not get_market_filter_config()["per_market_mode"]:
            return _calc_mode(jp, us)
        own = jp if self.market == "JP" else us
        return "normal" if own == 1 else "caution"

    def _index_flag(self, code: str, d: str, max_stale: int) -> int | None:
        series = self.data.indexes.get(code)
        if series is None:
            return None
        pos = bisect_right(series.dates, d) - 1
        while pos >= 0 and series.ma25[pos] is None:
            pos -= 1
        if pos < 0:
            return None
        row_date = series.dates[pos]
        if max_stale > 0 and (date.fromisoformat(d) - date.fromisoformat(row_date)).days > max_stale:
            return None
        return 1 if series.closes[pos] >= series.ma25[pos] else 0

    # ---------------- シグナル ----------------

    def signals(self, k: int) -> list[dict]:
        """通常モードの日に通知される候補（本番の通知枠と並び順）を返す。保有状況に依存しないので日ごとに使い回す。"""
        if k not in self._signal_cache:
            self._signal_cache[k] = self._compute_signals(k)
        return self._signal_cache[k]

    def _compute_signals(self, k: int) -> list[dict]:
        d = self.calendar[k]
        screened = self._screened_rows(k, d)
        if not screened:
            return []
        self._apply_rs_scores(k, d, screened)
        screened = apply_rs_filter(screened)

        swing: list[dict] = []
        breakout: list[dict] = []
        for name, is_swing in self._strategies:
            cands, _, _ = _evaluate_strategy(screened, d, "normal", name, is_swing=is_swing)
            if is_swing:
                swing = cands
            else:
                breakout = cands

        selected = select_notify_candidates(swing, breakout)
        for row in selected:
            td, i = self._locate(row["ticker"], k)
            row["support_level"] = td.support_resistance(i)[0]
            row.pop("_notify_extras", None)
        return selected

    def entry_set(self, signal: dict, open_count: int) -> dict:
        return calc_entry_set(signal, open_count, check_earnings=False)

    def _screened_rows(self, k: int, d: str) -> list[dict]:
        rows = []
        for t in self.data.screen[:, k].nonzero()[0]:
            td = self.data.tickers[t]
            rows.append(self._ticker_row(td, int(self.data.loc[t, k]), d))
        return rows

    def _ticker_row(self, td: TickerData, i: int, d: str) -> dict:
        """market_filter._liquidity_filter_sql の SELECT 結果に相当する辞書。"""
        row = {f: _value(td.ind[f][i]) for f in _TK_FIELDS}
        rsi_window = td.ind["rsi14"][max(0, i - self._rsi_lookback + 1):i + 1]
        row.update({
            "ticker": td.ticker,
            "market": self.market,
            "name": td.name,
            "date": d,
            "open": float(td.opens[i]),
            "close": float(td.closes[i]),
            "volume": float(td.volumes[i]),
            "rs_score": None,
            "support_level": None,
            "rsi_history": [float(v) for v in rsi_window if not math.isnan(v)],
        })
        return row

    def _apply_rs_scores(self, k: int, d: str, rows: list[dict]) -> None:
        """rs_score.update_rs_scores と同じ式で、その日の RS スコアを付ける。"""
        cfg = get_rs_score_config()
        if not cfg.get("enabled", True):
            return
        lookback = int(cfg.get("lookback_days", 126))
        bench_key = f"{self.market.lower()}_benchmark"
        benchmark = str(cfg.get(bench_key) or _MARKET_INDEX[self.market])
        index_ret = _index_return_pct(self.data.indexes.get(benchmark), d, lookback)
        if index_ret is None:
            return
        column = self.data.rs_returns[:, k]
        relative = [
            (self.data.rs_tickers[t], float(column[t]) - index_ret)
            for t in (~np.isnan(column)).nonzero()[0]
        ]
        scores = percentile_scores(relative)
        for row in rows:
            row["rs_score"] = scores.get(row["ticker"])

    def _locate(self, ticker: str, k: int) -> tuple[TickerData | None, int]:
        pos = self.data.ticker_pos.get(ticker)
        if pos is None:
            return None, -1
        i = int(self.data.loc[pos, k])
        if i < 0:
            return None, -1
        return self.data.tickers[pos], i

    @property
    def benchmark_index(self) -> str:
        return _MARKET_INDEX[self.market]

    def benchmark_return_pct(self) -> float | None:
        """シミュレーション期間の指数リターン（バイ・アンド・ホールド比較用）。"""
        series = self.data.indexes.get(self.benchmark_index)
        if series is None or not self.calendar:
            return None
        start = _close_on_or_before(series, self.calendar[0])
        end = _close_on_or_before(series, self.calendar[-1])
        if not start or end is None:
            return None
        return (end / start - 1) * 100

    def benchmark_yearly_pct(self) -> dict[str, float]:
        """年ごとの指数リターン（前年最終営業日比。初年は期間初日比）。"""
        series = self.data.indexes.get(self.benchmark_index)
        if series is None or not self.calendar:
            return {}
        year_end: dict[str, str] = {}
        for d in self.calendar:
            year_end[d[:4]] = d
        out = {}
        prev = _close_on_or_before(series, self.calendar[0])
        for year, d in year_end.items():
            close = _close_on_or_before(series, d)
            if prev and close is not None:
                out[year] = (close / prev - 1) * 100
            prev = close
        return out


def _index_return_pct(series: IndexSeries | None, d: str, lookback: int) -> float | None:
    """rs_score.index_return_pct と同じ（指定日以前の lookback 営業日リターン）。"""
    if series is None:
        return None
    pos = bisect_right(series.dates, d)
    if pos < lookback + 1:
        return None
    start_close = series.closes[pos - lookback - 1]
    if start_close <= 0:
        return None
    return (series.closes[pos - 1] / start_close - 1.0) * 100.0


def _close_on_or_before(series: IndexSeries, d: str) -> float | None:
    pos = bisect_right(series.dates, d) - 1
    return series.closes[pos] if pos >= 0 else None


def _value(v) -> float | None:
    v = float(v)
    return None if math.isnan(v) else v
