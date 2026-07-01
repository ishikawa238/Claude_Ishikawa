"""
市場の開場時間判定（ローカル時刻は JST 前提）。

US: 9:30–16:00 ET（DST 自動）
JP: 9:00–11:30 / 12:30–15:00 JST
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

_JST = ZoneInfo("Asia/Tokyo")
_ET = ZoneInfo("America/New_York")

_JP_SESSIONS = (
    (time(9, 0), time(11, 30)),
    (time(12, 30), time(15, 0)),
)


def _now_jst(now: datetime | None = None) -> datetime:
    current = now or datetime.now(_JST)
    if current.tzinfo is None:
        return current.replace(tzinfo=_JST)
    return current.astimezone(_JST)


def is_weekday(d: date) -> bool:
    return d.weekday() < 5


def is_jp_market_open(now: datetime | None = None) -> bool:
    """日本株の取引時間中か。"""
    jst = _now_jst(now)
    if not is_weekday(jst.date()):
        return False
    t = jst.time()
    return any(start <= t < end for start, end in _JP_SESSIONS)


def is_us_market_open(now: datetime | None = None) -> bool:
    """米国株の取引時間中か（ET 9:30–16:00）。"""
    jst = _now_jst(now)
    et = jst.astimezone(_ET)
    if not is_weekday(et.date()):
        return False
    t = et.time()
    return time(9, 30) <= t < time(16, 0)


def open_markets(markets: list[str], now: datetime | None = None) -> list[str]:
    """開場中の市場コード（US / JP）を返す。"""
    allowed = {m.upper() for m in markets}
    open_list: list[str] = []
    if "US" in allowed and is_us_market_open(now):
        open_list.append("US")
    if "JP" in allowed and is_jp_market_open(now):
        open_list.append("JP")
    return open_list


def any_market_open(markets: list[str], now: datetime | None = None) -> bool:
    return bool(open_markets(markets, now))
