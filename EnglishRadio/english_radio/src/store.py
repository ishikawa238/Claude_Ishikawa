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
from src.study.store import init_study_schema

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
        play_count    INTEGER NOT NULL DEFAULT 0,
        image_url     TEXT,
        art_file      TEXT,
        summary       TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_episodes_show ON episodes(show)",
    """
    CREATE TABLE IF NOT EXISTS shows (
        name      TEXT PRIMARY KEY,
        image_url TEXT,
        art_file  TEXT,
        color     TEXT
    )
    """,
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


# 後から追加した列（古い DB には ALTER TABLE で足す）
_ADDED_COLUMNS = {"episodes": {"image_url": "TEXT", "art_file": "TEXT", "summary": "TEXT"}}

# エピソードに番組のアートワークと代表色を付けて返すための共通 SELECT
_EPISODE_SELECT = (
    "SELECT e.*, s.art_file AS show_art, s.color AS show_color"
    " FROM episodes e LEFT JOIN shows s ON s.name = e.show"
)


def init_db(conn: sqlite3.Connection) -> None:
    for sql in SCHEMA:
        conn.execute(sql)
    init_study_schema(conn)
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, col_type in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")


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
        "image_url": entry.get("image_url") or None,
        "art_file": entry.get("art_file") or None,
        "summary": entry.get("summary") or None,
    }
    insert_one(conn, "episodes", row)


def set_episode_art(conn: sqlite3.Connection, guid: str, image_url: str, art_file: str | None,
                    summary: str | None = None) -> None:
    """アートワークと紹介文を後から埋める（summary は空のときだけ上書き）。"""
    conn.execute(
        "UPDATE episodes SET image_url = ?, art_file = ?,"
        " summary = COALESCE(NULLIF(summary, ''), ?) WHERE guid = ?",
        (image_url, art_file, summary, guid),
    )


def episodes_missing_art(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = fetch_all(
        conn,
        "SELECT * FROM episodes WHERE image_url IS NOT NULL AND image_url != ''"
        " AND (art_file IS NULL OR art_file = '') LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# shows（番組のアートワークと代表色）
# ---------------------------------------------------------------------------

def upsert_show(conn: sqlite3.Connection, name: str, image_url: str, art_file: str | None,
                color: str | None) -> None:
    conn.execute(
        "INSERT INTO shows (name, image_url, art_file, color) VALUES (?,?,?,?)"
        " ON CONFLICT(name) DO UPDATE SET image_url = excluded.image_url,"
        " art_file = COALESCE(excluded.art_file, shows.art_file),"
        " color = COALESCE(excluded.color, shows.color)",
        (name, image_url, art_file, color),
    )


def get_show(conn: sqlite3.Connection, name: str) -> dict | None:
    row = fetch_one(conn, "SELECT * FROM shows WHERE name = ?", (name,))
    return dict(row) if row else None


def list_shows(conn: sqlite3.Connection) -> dict[str, dict]:
    return {r["name"]: dict(r) for r in fetch_all(conn, "SELECT * FROM shows")}


def get_episode(conn: sqlite3.Connection, guid: str) -> dict | None:
    row = fetch_one(conn, f"{_EPISODE_SELECT} WHERE e.guid = ?", (guid,))
    return dict(row) if row else None


def list_episodes(
    conn: sqlite3.Connection, show: str | None = None, query: str | None = None
) -> list[dict]:
    sql = f"{_EPISODE_SELECT} WHERE 1=1"
    params: list = []
    if show:
        sql += " AND e.show = ?"
        params.append(show)
    if query:
        sql += " AND e.title LIKE ?"
        params.append(f"%{query}%")
    sql += " ORDER BY e.published DESC"
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
        "SELECT e.show, COUNT(*) AS cnt, COALESCE(SUM(e.duration_sec),0) AS dur,"
        " SUM(CASE WHEN e.play_count = 0 THEN 1 ELSE 0 END) AS unplayed,"
        " s.art_file AS show_art, s.color AS show_color"
        " FROM episodes e LEFT JOIN shows s ON s.name = e.show"
        " GROUP BY e.show ORDER BY cnt DESC",
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
        "SELECT i.position, i.source, e.*, s.art_file AS show_art, s.color AS show_color"
        " FROM play_history_items i JOIN episodes e ON e.guid = i.guid"
        " LEFT JOIN shows s ON s.name = e.show WHERE i.history_id = ? ORDER BY i.position",
        (history_id,),
    )
    return {**dict(head), "items": [dict(r) for r in items]}


def history_artworks(conn: sqlite3.Connection, history_ids: list[int], per_history: int = 4) -> dict[int, list[dict]]:
    """各履歴の先頭数本のアートワーク情報（モザイク表示用）を返す。"""
    if not history_ids:
        return {}
    marks = ",".join("?" * len(history_ids))
    rows = fetch_all(
        conn,
        "SELECT i.history_id, e.show, e.art_file, s.art_file AS show_art, s.color AS show_color"
        " FROM play_history_items i JOIN episodes e ON e.guid = i.guid"
        f" LEFT JOIN shows s ON s.name = e.show WHERE i.history_id IN ({marks})"
        " ORDER BY i.history_id, i.position",
        tuple(history_ids),
    )
    result: dict[int, list[dict]] = {}
    for row in rows:
        items = result.setdefault(row["history_id"], [])
        if len(items) < per_history:
            items.append(dict(row))
    return result


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


def recent_batches(conn: sqlite3.Connection, limit: int = 7, batch_type: str = "morning") -> list[dict]:
    rows = fetch_all(
        conn,
        "SELECT * FROM batch_run_log WHERE batch_type = ? ORDER BY start_time DESC, id DESC LIMIT ?",
        (batch_type, limit),
    )
    return [dict(r) for r in rows]


def today_batch(conn: sqlite3.Connection, batch_type: str = "morning") -> dict | None:
    today = datetime.now().strftime("%Y-%m-%d")
    row = fetch_one(
        conn,
        "SELECT * FROM batch_run_log WHERE run_date = ? AND batch_type = ? ORDER BY id DESC LIMIT 1",
        (today, batch_type),
    )
    return dict(row) if row else None


def log_error(conn: sqlite3.Connection, module: str, error: Exception, fatal: bool,
              batch_type: str = "morning") -> None:
    insert_one(conn, "error_log", {
        "error_time": now_str(),
        "batch_type": batch_type,
        "module": module,
        "error_type": type(error).__name__,
        "error_message": str(error)[:1000],
        "is_fatal": 1 if fatal else 0,
    })
