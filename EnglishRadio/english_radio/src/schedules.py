"""
タイムスケジュールの判定と検証。

schedule の形式:
  {"label": "Morning English", "start": "07:00", "end": "08:30", "days": "daily"}
  days は "daily"（毎日）/ "weekdays"（平日）/ "weekends"（土日）
"""

import re
from datetime import datetime

DAYS_OPTIONS = {"daily": "毎日", "weekdays": "平日のみ", "weekends": "土日のみ"}
MATCH_WINDOW_MIN = 10
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def to_minutes(hhmm: str) -> int:
    m = _TIME_RE.match(hhmm or "")
    if not m:
        raise ValueError(f"時刻は HH:MM 形式で入力してください: '{hhmm}'")
    return int(m.group(1)) * 60 + int(m.group(2))


def duration_minutes(schedule: dict) -> int:
    return to_minutes(schedule["end"]) - to_minutes(schedule["start"])


def runs_on(schedule: dict, when: datetime) -> bool:
    days = schedule.get("days", "daily")
    is_weekend = when.weekday() >= 5
    if days == "weekdays":
        return not is_weekend
    if days == "weekends":
        return is_weekend
    return True


def active_schedule(schedules: list[dict], now: datetime | None = None) -> dict | None:
    """現在時刻の前後 10 分以内に開始するスケジュールを返す。"""
    now = now or datetime.now()
    current = now.hour * 60 + now.minute
    for sched in schedules:
        try:
            start = to_minutes(sched.get("start", ""))
        except ValueError:
            continue
        if abs(current - start) <= MATCH_WINDOW_MIN and runs_on(sched, now):
            return sched
    return None


def validate_schedules(raw: list) -> list[dict]:
    """Web から受け取ったスケジュールを検証し、整形して返す。不正なら ValueError。"""
    if not isinstance(raw, list) or not raw:
        raise ValueError("スケジュールを1件以上登録してください")
    cleaned = []
    starts = set()
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"{i}件目の形式が正しくありません")
        start = str(item.get("start", "")).strip()
        end = str(item.get("end", "")).strip()
        if to_minutes(end) <= to_minutes(start):
            raise ValueError(f"{i}件目: 終了時刻は開始時刻より後にしてください")
        if start in starts:
            raise ValueError(f"開始時刻 {start} が重複しています")
        starts.add(start)
        days = item.get("days", "daily")
        if days not in DAYS_OPTIONS:
            raise ValueError(f"{i}件目: 曜日の指定が正しくありません")
        label = str(item.get("label", "")).strip()[:40] or f"{start} の英語ラジオ"
        cleaned.append({"label": label, "start": start, "end": end, "days": days})
    return sorted(cleaned, key=lambda s: s["start"])


def next_occurrence(schedules: list[dict], now: datetime | None = None) -> tuple[datetime, dict] | None:
    """次に再生される日時とスケジュールを返す（7日以内）。"""
    from datetime import timedelta

    now = now or datetime.now()
    best: tuple[datetime, dict] | None = None
    for day_offset in range(8):
        day = (now + timedelta(days=day_offset)).replace(second=0, microsecond=0)
        for sched in schedules:
            try:
                minutes = to_minutes(sched["start"])
            except (KeyError, ValueError):
                continue
            at = day.replace(hour=minutes // 60, minute=minutes % 60)
            if at > now and runs_on(sched, at) and (best is None or at < best[0]):
                best = (at, sched)
        if best:
            return best
    return best
