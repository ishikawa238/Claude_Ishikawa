"""
SQLite アクセス層。

テーブル:
  episodes            ダウンロード済みエピソード（= ライブラリ）
  play_history        1回の再生（朝バッチ / 手動）= 1行
  play_history_items  その回に流したエピソードの並び
  batch_run_log       実行ログ（shared/batch.py と watchdog の共通形式）
  error_log           致命的エラー（watchdog の共通形式）
"""

import sqlite3
from datetime import datetime

from shared.batch import now_str
from shared.db import fetch_all, fetch_one, insert_one

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS episodes (
        guid          TEXT PRIMARY KEY,
        show          TEXT NOT NULL,
        title         TEXT NOT NULL,
        audio_url     TEXT,
        path          TEXT NOT NULL,
        published     TEXT,
        duration_sec  INTEGER,
        size_bytes    INTEGER DEFAULT 0,
        downloaded_at TEXT,
        last_played   TEXT,
        play_count    INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_episodes_show ON episodes(show)",
    """
    CREATE TABLE IF NOT EXISTS play_history (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        played_at      TEXT NOT NULL,
        schedule_label TEXT,
        target_minutes INTEGER,
        total_sec      INTEGER DEFAULT 0,
        status         TEXT,
        trigger        TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS play_history_items (
        history_id INTEGER NOT NULL REFERENCES play_history(id) ON DELETE CASCADE,
        position   INTEGER NOT NULL,
        guid       TEXT NOT NULL,
        source     TEXT NOT NULL CHECK(source IN ('new','shuffle')),
        PRIMARY KEY (history_id, position)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS batch_run_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        run_date        DATE NOT NULL,
        batch_type      TEXT NOT NULL DEFAULT 'morning',
        start_time      DATETIME,
        end_time        DATETIME,
        status          TEXT CHECK(status IN ('running','success','partial_failure','failed')),
        new_episodes    INTEGER DEFAULT 0,
        queued_episodes INTEGER DEFAULT 0,
        message         TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_batch_run_log_date ON batch_run_log(run_date)",
    """
    CREATE TABLE IF NOT EXISTS error_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        error_time    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        batch_type    TEXT,
        module        TEXT,
        error_type    TEXT,
        error_message TEXT,
        is_fatal      INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_error_log_time ON error_log(error_time)",
]


def init_db(conn: sqlite3.Connection) -> None:
    for sql in SCHEMA:
        conn.execute(sql)


# ---------------------------------------------------------------------------
# episodes
# ---------------------------------------------------------------------------

def known_guids(conn: sqlite3.Connection) -> set[str]:
    return {row["guid"] for row in fetch_all(conn, "SELECT guid FROM episodes")}


def add_episode(conn: sqlite3.Connection, entry: dict) -> None:
    row = {
        "guid": entry["guid"],
        "show": entry["show"],
        "title": entry["title"],
        "audio_url": entry.get("audio_url"),
        "path": entry["path"],
        "published": entry.get("published", ""),
        "duration_sec": entry.get("duration_sec"),
        "size_bytes": entry.get("size_bytes", 0),
        "downloaded_at": entry.get("downloaded_at") or now_str(),
    }
    insert_one(conn, "episodes", row)


def get_episode(conn: sqlite3.Connection, guid: str) -> dict | None:
    row = fetch_one(conn, "SELECT * FROM episodes WHERE guid = ?", (guid,))
    return dict(row) if row else None


def list_episodes(
    conn: sqlite3.Connection, show: str | None = None, query: str | None = None
) -> list[dict]:
    sql = "SELECT * FROM episodes WHERE 1=1"
    params: list = []
    if show:
        sql += " AND show = ?"
        params.append(show)
    if query:
        sql += " AND title LIKE ?"
        params.append(f"%{query}%")
    sql += " ORDER BY published DESC"
    return [dict(r) for r in fetch_all(conn, sql, tuple(params))]


def mark_played(conn: sqlite3.Connection, guids: list[str], when: str | None = None) -> None:
    played_at = when or now_str()
    conn.executemany(
        "UPDATE episodes SET last_played = ?, play_count = play_count + 1 WHERE guid = ?",
        [(played_at, g) for g in guids],
    )


def library_stats(conn: sqlite3.Connection) -> dict:
    total = fetch_one(
        conn,
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(size_bytes),0) AS size,"
        " COALESCE(SUM(duration_sec),0) AS dur,"
        " SUM(CASE WHEN play_count = 0 THEN 1 ELSE 0 END) AS unplayed FROM episodes",
    )
    by_show = fetch_all(
        conn,
        "SELECT show, COUNT(*) AS cnt, COALESCE(SUM(duration_sec),0) AS dur"
        " FROM episodes GROUP BY show ORDER BY cnt DESC",
    )
    return {
        "count": total["cnt"],
        "size_bytes": total["size"],
        "duration_sec": total["dur"],
        "unplayed": total["unplayed"] or 0,
        "by_show": [dict(r) for r in by_show],
    }


# ---------------------------------------------------------------------------
# play_history
# ---------------------------------------------------------------------------

def save_history(
    conn: sqlite3.Connection,
    queue: list[tuple[dict, str]],
    *,
    schedule_label: str,
    target_minutes: int,
    trigger: str,
    status: str = "played",
) -> int:
    """再生したキュー [(entry, source)] を履歴に保存し、id を返す。"""
    total = sum(entry.get("duration_sec") or 0 for entry, _ in queue)
    history_id = insert_one(conn, "play_history", {
        "played_at": now_str(),
        "schedule_label": schedule_label,
        "target_minutes": target_minutes,
        "total_sec": total,
        "status": status,
        "trigger": trigger,
    })
    conn.executemany(
        "INSERT INTO play_history_items (history_id, position, guid, source) VALUES (?,?,?,?)",
        [(history_id, i, entry["guid"], source) for i, (entry, source) in enumerate(queue)],
    )
    return history_id


def list_history(conn: sqlite3.Connection, limit: int = 20, offset: int = 0) -> list[dict]:
    rows = fetch_all(
        conn,
        "SELECT h.*, COUNT(i.guid) AS item_count FROM play_history h"
        " LEFT JOIN play_history_items i ON i.history_id = h.id"
        " GROUP BY h.id ORDER BY h.played_at DESC, h.id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return [dict(r) for r in rows]


def count_history(conn: sqlite3.Connection) -> int:
    return fetch_one(conn, "SELECT COUNT(*) AS cnt FROM play_history")["cnt"]


def get_history(conn: sqlite3.Connection, history_id: int) -> dict | None:
    head = fetch_one(conn, "SELECT * FROM play_history WHERE id = ?", (history_id,))
    if not head:
        return None
    items = fetch_all(
        conn,
        "SELECT i.position, i.source, e.* FROM play_history_items i"
        " JOIN episodes e ON e.guid = i.guid WHERE i.history_id = ? ORDER BY i.position",
        (history_id,),
    )
    return {**dict(head), "items": [dict(r) for r in items]}


def get_latest_history(conn: sqlite3.Connection) -> dict | None:
    row = fetch_one(conn, "SELECT id FROM play_history ORDER BY played_at DESC, id DESC LIMIT 1")
    return get_history(conn, row["id"]) if row else None


# ---------------------------------------------------------------------------
# batch_run_log / error_log
# ---------------------------------------------------------------------------

def is_batch_running(conn: sqlite3.Connection, batch_type: str = "morning") -> bool:
    row = fetch_one(
        conn,
        "SELECT COUNT(*) AS cnt FROM batch_run_log WHERE batch_type = ? AND status = 'running'",
        (batch_type,),
    )
    return row["cnt"] > 0


def recent_batches(conn: sqlite3.Connection, limit: int = 7) -> list[dict]:
    rows = fetch_all(
        conn, "SELECT * FROM batch_run_log ORDER BY start_time DESC, id DESC LIMIT ?", (limit,)
    )
    return [dict(r) for r in rows]


def today_batch(conn: sqlite3.Connection) -> dict | None:
    today = datetime.now().strftime("%Y-%m-%d")
    row = fetch_one(
        conn,
        "SELECT * FROM batch_run_log WHERE run_date = ? ORDER BY id DESC LIMIT 1",
        (today,),
    )
    return dict(row) if row else None


def log_error(conn: sqlite3.Connection, module: str, error: Exception, fatal: bool) -> None:
    insert_one(conn, "error_log", {
        "error_time": now_str(),
        "batch_type": "morning",
        "module": module,
        "error_type": type(error).__name__,
        "error_message": str(error)[:1000],
        "is_fatal": 1 if fatal else 0,
    })
