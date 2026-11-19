"""
再生キューの選曲。

1. 新着を番組ごとのラウンドロビンで、目標時間に収まるだけ入れる
2. 時間が余ったら、ライブラリから「未再生 → 再生が古い順」を優先してシャッフルで埋める
"""

import random
from collections import OrderedDict

NEW = "new"
SHUFFLE = "shuffle"


def _fits(entry: dict, total: int, target: int) -> bool:
    dur = entry.get("duration_sec")
    return bool(dur) and total + dur <= target


def pick_new(new_entries: list[dict], target_sec: int) -> list[dict]:
    """新着を番組ごとに新しい順で並べ、各番組から1本ずつ順番に追加する。"""
    by_show: OrderedDict[str, list[dict]] = OrderedDict()
    ordered = sorted(new_entries, key=lambda e: e.get("published") or "", reverse=True)
    for entry in ordered:
        by_show.setdefault(entry["show"], []).append(entry)

    selected: list[dict] = []
    total = 0
    rounds = max((len(v) for v in by_show.values()), default=0)
    for idx in range(rounds):
        for eps in by_show.values():
            if idx < len(eps) and _fits(eps[idx], total, target_sec):
                selected.append(eps[idx])
                total += eps[idx]["duration_sec"]
    return selected


def pick_shuffle(
    library: list[dict], exclude: set[str], remaining_sec: int, rng: random.Random
) -> list[dict]:
    """ライブラリから残り時間を埋める。未再生と、しばらく聞いていないものを優先する。"""
    pool = [e for e in library if e["guid"] not in exclude and e.get("duration_sec")]
    rng.shuffle(pool)
    # 未再生（last_played なし）を先頭に、その後は最終再生が古い順。同順位はシャッフル順のまま。
    pool.sort(key=lambda e: (e.get("last_played") is not None, e.get("last_played") or ""))

    selected: list[dict] = []
    total = 0
    for entry in pool:
        if _fits(entry, total, remaining_sec):
            selected.append(entry)
            total += entry["duration_sec"]
    return selected


def build_queue(
    new_entries: list[dict],
    library: list[dict],
    target_minutes: int,
    rng: random.Random | None = None,
) -> list[tuple[dict, str]]:
    """[(entry, "new" | "shuffle")] の再生キューを返す。"""
    rng = rng or random.Random()
    target_sec = target_minutes * 60
    fresh = pick_new(new_entries, target_sec)
    used = sum(e["duration_sec"] for e in fresh)
    exclude = {e["guid"] for e in fresh}
    filler = pick_shuffle(library, exclude, target_sec - used, rng)
    return [(e, NEW) for e in fresh] + [(e, SHUFFLE) for e in filler]
