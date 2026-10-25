"""
バックテスト用の市場データ。

price_history / index_snapshot / universe_master を読み取り専用で読み、
銘柄ごとの指標と「その日に流動性フィルタを通るか」を事前に計算しておく。
"""

from __future__ import annotations

import sqlite3
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from src.config import CONFIG, get_rs_score_config
from src.logger import get_logger
from src.market_filter import screening_thresholds
from src.technical_calculator import calculate_index_mas
from src.universe_prefilter import get_prefilter_config

from .indicators import (
    compute_indicators,
    price_jump_flags,
    return_series,
    support_resistance_at,
)

logger = get_logger(__name__)

# technical_calculator.calculate_and_save_snapshots と同じ読み込み幅（暦日）
WARMUP_CALENDAR_DAYS = 756
# 営業日とみなす日の条件: 読み込んだ銘柄のうち、価格がある銘柄数が最大日のこの割合以上
_CALENDAR_MIN_COVERAGE = 0.5
# 直近この本数以内に価格の不連続がある銘柄は、指標が歪んでいるため候補にしない
_JUMP_LOOKBACK_BARS = 130
_PROGRESS_EVERY = 1000
_PREFILTER_LOOKBACK_DAYS = int(get_prefilter_config().get("lookback_days", 90))


@dataclass
class TickerData:
    """候補になり得る銘柄1つ分の価格と指標（配列は銘柄自身の取引日順）。"""

    ticker: str
    name: str | None
    shares: float | None
    dates: list[str]
    opens: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    closes: np.ndarray
    volumes: np.ndarray
    ind: dict[str, np.ndarray]
    screen_ok: np.ndarray
    jump: np.ndarray
    _sr_cache: dict = field(default_factory=dict, repr=False)

    def support_resistance(self, i: int) -> tuple[float | None, float | None]:
        if i not in self._sr_cache:
            self._sr_cache[i] = support_resistance_at(self.closes, self.highs, self.lows, i)
        return self._sr_cache[i]


@dataclass
class IndexSeries:
    dates: list[str]
    closes: list[float]
    ma25: list[float | None]


@dataclass
class MarketData:
    market: str
    calendar: list[str]
    tickers: list[TickerData]
    loc: np.ndarray            # [銘柄, 営業日] → 銘柄内の行番号。価格が無い日は -1
    screen: np.ndarray         # [銘柄, 営業日] → 流動性フィルタ通過（不連続銘柄は除外済み）
    rs_returns: np.ndarray     # [RS 母集団, 営業日] → 126 日騰落率。価格が無い日は NaN
    rs_tickers: list[str]
    indexes: dict[str, IndexSeries]
    ticker_pos: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.ticker_pos = {t.ticker: n for n, t in enumerate(self.tickers)}

    def row_index(self, ticker: str, k: int) -> int:
        pos = self.ticker_pos.get(ticker)
        if pos is None:
            return -1
        return int(self.loc[pos, k])


# ============================================================
# 読み込み
# ============================================================

def load_market_data(
    market: str,
    start: str,
    end: str,
    db_path: str | Path | None = None,
) -> MarketData:
    """指定市場・期間のバックテスト用データを DB から組み立てる。"""
    load_from = (date.fromisoformat(start) - timedelta(days=WARMUP_CALENDAR_DAYS)).isoformat()
    thresholds = screening_thresholds(market)
    rs_min_turnover = _rs_universe_min_turnover(market, thresholds)

    conn = _connect_readonly(db_path or CONFIG["database"]["db_path"])
    try:
        universe = conn.execute(
            "SELECT ticker, name, shares_outstanding FROM universe_master "
            "WHERE market = ? AND is_active = 1 AND is_excluded = 0 ORDER BY ticker",
            (market,),
        ).fetchall()
        logger.info(f"価格データ読み込み開始: {market} {len(universe):,} 銘柄 ({load_from} ～ {end})")

        date_counts: Counter = Counter()
        rs_entries: list[tuple[str, list[str], np.ndarray]] = []
        tradable: list[TickerData] = []
        for n, (ticker, name, shares) in enumerate(universe, start=1):
            bars = _load_bars(conn, ticker, load_from, end)
            if bars is not None:
                dates, opens, highs, lows, closes, volumes = bars
                turnovers = [c * v for c, v in zip(closes, volumes)]
                if not rs_min_turnover or max(turnovers) >= rs_min_turnover:
                    date_counts.update(dates)
                    returns = return_series(closes)
                    if rs_min_turnover:
                        recent = _prior_window_max(dates, turnovers, _PREFILTER_LOOKBACK_DAYS)
                        returns[recent < rs_min_turnover] = np.nan
                    rs_entries.append((ticker, dates, returns))
                    if _ever_passes_liquidity(dates, closes, volumes, thresholds, start, end):
                        tradable.append(_build_ticker(
                            ticker, name, shares, bars, thresholds,
                        ))
            if n % _PROGRESS_EVERY == 0:
                logger.info(f"  読み込み {n:,}/{len(universe):,} 銘柄（候補対象 {len(tradable):,}）")

        indexes = {code: _load_index(conn, code, end) for code in _index_codes()}
    finally:
        conn.close()

    calendar = build_calendar(date_counts, start, end)
    loc, screen = _align_to_calendar(tradable, calendar)
    rs_returns = _align_returns(rs_entries, calendar)
    logger.info(
        f"価格データ読み込み完了: 営業日 {len(calendar)} 日 / 候補対象 {len(tradable):,} 銘柄 / "
        f"RS 母集団 {len(rs_entries):,} 銘柄"
    )
    return MarketData(
        market=market,
        calendar=calendar,
        tickers=tradable,
        loc=loc,
        screen=screen,
        rs_returns=rs_returns,
        rs_tickers=[t for t, _, _ in rs_entries],
        indexes=indexes,
    )


def build_calendar(date_counts: Counter, start: str, end: str) -> list[str]:
    """価格がある銘柄数が十分な日だけを営業日として昇順で返す。"""
    in_range = {d: c for d, c in date_counts.items() if start <= d <= end}
    if not in_range:
        return []
    min_count = max(in_range.values()) * _CALENDAR_MIN_COVERAGE
    return sorted(d for d, c in in_range.items() if c >= min_count)


def _connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _load_bars(conn, ticker: str, load_from: str, end: str):
    """technical_calculator._load_prices と同じ条件で読み、終値の無い行を除く。"""
    rows = conn.execute(
        "SELECT date, open, high, low, adj_close, volume FROM price_history "
        "WHERE ticker = ? AND date BETWEEN ? AND ? AND is_holiday = 0 ORDER BY date",
        (ticker, load_from, end),
    ).fetchall()
    rows = [r for r in rows if r[4]]
    if not rows:
        return None
    closes = [float(r[4]) for r in rows]
    return (
        [r[0] for r in rows],
        [float(r[1]) if r[1] else c for r, c in zip(rows, closes)],
        [float(r[2]) if r[2] else c for r, c in zip(rows, closes)],
        [float(r[3]) if r[3] else c for r, c in zip(rows, closes)],
        closes,
        [float(r[5]) if r[5] else 0.0 for r in rows],
    )


def _rs_universe_min_turnover(market: str, thresholds: dict) -> float:
    """
    RS スコアの母集団に入れる売買代金の下限（0 なら全銘柄）。

    本番の US は平日、事前フィルタ（universe_prefilter）を通った銘柄だけテクニカルを計算するため、
    RS の順位もその母集団で付く。週1回の全銘柄取得日の母集団の違いは再現しない。
    """
    cfg = get_prefilter_config()
    markets = [str(m).upper() for m in cfg.get("markets", [])]
    if not cfg.get("enabled", True) or market.upper() not in markets:
        return 0.0
    return float(thresholds["min_turnover"]) * float(cfg.get("min_turnover_ratio", 0.25))


def _prior_window_max(dates: list[str], values: list[float], window_days: int) -> np.ndarray:
    """各日について、前日までの直近 window_days 暦日の最大値（データが無ければ +inf = 判断材料なし）。"""
    days = [date.fromisoformat(d) for d in dates]
    out = np.full(len(values), np.inf)
    window: deque[int] = deque()
    for i in range(len(values)):
        lower = days[i] - timedelta(days=window_days)
        while window and days[window[0]] < lower:
            window.popleft()
        if window:
            out[i] = values[window[0]]
        while window and values[window[-1]] <= values[i]:
            window.pop()
        window.append(i)
    return out


def _ever_passes_liquidity(dates, closes, volumes, th: dict, start: str, end: str) -> bool:
    """期間中に株価帯と売買代金の条件を一度でも満たすか（満たさない銘柄は候補になり得ない）。"""
    max_price = th["max_price"] if th["max_price"] is not None else float("inf")
    for d, c, v in zip(dates, closes, volumes):
        if start <= d <= end and th["min_price"] <= c <= max_price and v > 0 \
                and c * v >= th["min_turnover"]:
            return True
    return False


def _build_ticker(ticker, name, shares, bars, th: dict) -> TickerData:
    dates, opens, highs, lows, closes, volumes = bars
    ind = compute_indicators(dates, highs, lows, closes, volumes)
    close_arr = np.array(closes)
    vol_arr = np.array(volumes)
    jump = price_jump_flags(closes)
    return TickerData(
        ticker=ticker,
        name=name,
        shares=float(shares) if shares else None,
        dates=dates,
        opens=np.array(opens),
        highs=np.array(highs),
        lows=np.array(lows),
        closes=close_arr,
        volumes=vol_arr,
        ind=ind,
        screen_ok=_screen_ok(close_arr, vol_arr, ind, shares, th) & ~_recent_flags(jump),
        jump=jump,
    )


def _screen_ok(close, volume, ind: dict, shares, th: dict) -> np.ndarray:
    """market_filter.get_screened_tickers の条件（RS 足切りを除く）を日ごとに判定する。"""
    turnover = np.where(volume > 0, close * volume, np.nan)
    ok = (close >= th["min_price"]) & (turnover >= th["min_turnover"])
    if th["max_price"] is not None:
        ok &= close <= th["max_price"]
    ok &= ~np.isnan(ind["ema25"]) & ~np.isnan(ind["ema75"])
    ok &= ind["active_days"] >= th["min_active_days"]
    if shares:
        ok &= close * float(shares) >= th["min_market_cap"]
    elif not th["allow_unknown_market_cap"]:
        ok &= False
    return ok


def _recent_flags(flags: np.ndarray) -> np.ndarray:
    """直近 _JUMP_LOOKBACK_BARS 本以内に True があるか。"""
    csum = np.cumsum(flags.astype(np.int32))
    lagged = np.concatenate([np.zeros(_JUMP_LOOKBACK_BARS, dtype=csum.dtype), csum])[:len(csum)]
    return (csum - lagged) > 0


def _align_to_calendar(tickers: list[TickerData], calendar: list[str]):
    pos = {d: k for k, d in enumerate(calendar)}
    loc = np.full((len(tickers), len(calendar)), -1, dtype=np.int32)
    screen = np.zeros((len(tickers), len(calendar)), dtype=bool)
    for t, td in enumerate(tickers):
        for i, d in enumerate(td.dates):
            k = pos.get(d)
            if k is not None:
                loc[t, k] = i
                screen[t, k] = td.screen_ok[i]
    return loc, screen


def _align_returns(entries, calendar: list[str]) -> np.ndarray:
    pos = {d: k for k, d in enumerate(calendar)}
    out = np.full((len(entries), len(calendar)), np.nan)
    for t, (_, dates, returns) in enumerate(entries):
        for d, r in zip(dates, returns):
            k = pos.get(d)
            if k is not None:
                out[t, k] = r
    return out


def _index_codes() -> list[str]:
    rs_cfg = get_rs_score_config()
    codes = ["N225", "SP500", str(rs_cfg.get("jp_benchmark")), str(rs_cfg.get("us_benchmark"))]
    return list(dict.fromkeys(c for c in codes if c and c != "None"))


def _load_index(conn, code: str, end: str) -> IndexSeries:
    """指数終値を読み、MA25 は technical_calculator と同じ関数で計算し直す。"""
    rows = conn.execute(
        "SELECT date, close FROM index_snapshot "
        "WHERE index_code = ? AND date <= ? AND close IS NOT NULL ORDER BY date",
        (code, end),
    ).fetchall()
    computed = calculate_index_mas(code, [{"date": d, "close": c} for d, c in rows])
    return IndexSeries(
        dates=[r["date"] for r in computed],
        closes=[float(r["close"]) for r in computed],
        ma25=[r["ma25"] for r in computed],
    )
