"""
ダッシュボード（稼働状況）用のデータ集計。
"""

import time
from datetime import datetime

from shared.config import get_config
from shared.db import get_connection
from src import paths, player, scheduler_info, store
from src.study import store as study_store
from src.schedules import next_occurrence

_TASK_CACHE: dict = {}
_TASK_CACHE_SEC = 30

TODAY_STATES = {
    "success": ("green", "🟢", "今日の英語ラジオは再生済みです"),
    "running": ("blue", "🔵", "いま準備中です"),
    "failed": ("red", "🔴", "今日は失敗しました"),
    "partial_failure": ("yellow", "🟡", "一部の番組で失敗しました"),
    None: ("yellow", "🟡", "今日はまだ再生していません"),
}


def task_status(task_name: str, use_cache: bool = True) -> dict:
    """タスクの状態（PowerShell 呼び出しは重いので 30 秒キャッシュ）。"""
    cached = _TASK_CACHE.get(task_name)
    if use_cache and cached and time.time() - cached[0] < _TASK_CACHE_SEC:
        return cached[1]
    status = scheduler_info.get_task_status(task_name)
    _TASK_CACHE[task_name] = (time.time(), status)
    return status


def clear_task_cache() -> None:
    _TASK_CACHE.clear()


def recent_errors(limit: int = 5) -> list[str]:
    log_file = paths.log_dir() / f"batch_{datetime.now():%Y%m%d}.log"
    if not log_file.exists():
        return []
    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    return [l for l in lines if "[ERROR]" in l or "[WARNING]" in l][-limit:]


def today_state(batch: dict | None, latest: dict | None = None) -> dict:
    key = batch["status"] if batch else None
    color, icon, text = TODAY_STATES.get(key, TODAY_STATES[None])
    # --no-play で準備だけした日は「再生済み」と言わない
    if key == "success" and latest and latest.get("status") == "queued":
        color, icon, text = "yellow", "🟡", "リストは準備済みです（まだ再生していません）"
    return {"color": color, "icon": icon, "text": text, "batch": batch}


def dashboard() -> dict:
    radio_cfg = get_config().get("radio", {})
    with get_connection(paths.db_path()) as conn:
        batch = store.today_batch(conn)
        recent = store.recent_batches(conn, 7)
        stats = store.library_stats(conn)
        latest = store.get_latest_history(conn)
        study = study_store.study_stats(conn)
    nxt = next_occurrence(radio_cfg.get("schedules", []))
    feeds = radio_cfg.get("feeds", [])
    return {
        "today": today_state(batch, latest),
        "next": {"at": nxt[0], "label": nxt[1].get("label", "")} if nxt else None,
        "task": task_status(scheduler_info.MORNING_TASK),
        "web_task": task_status(scheduler_info.WEB_TASK),
        "recent": recent,
        "stats": stats,
        "latest": latest,
        "vlc": player.find_vlc(radio_cfg.get("vlc_path")),
        "errors": recent_errors(),
        "feed_count": sum(1 for f in feeds if f.get("enabled", True)),
        "study": study if study["ready"] or study["vocab"] else None,
    }
