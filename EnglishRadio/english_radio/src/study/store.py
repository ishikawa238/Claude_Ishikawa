"""
学習機能の SQLite アクセス層。

テーブル:
  transcripts          エピソードごとの文字起こし状態と日本語要約
  transcript_segments  1文 = 1行（開始/終了秒・英文・和訳）
  vocab                覚えるべき単語・フレーズ（Leitner 方式で復習）
"""

import sqlite3
from datetime import date, timedelta

from shared.batch import now_str
from shared.db import fetch_all, fetch_one

STUDY_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS transcripts (
        guid       TEXT PRIMARY KEY,
        status     TEXT NOT NULL CHECK(status IN ('done','failed')),
        ai_status  TEXT NOT NULL DEFAULT 'none' CHECK(ai_status IN ('none','done','failed','skipped')),
        model      TEXT,
        summary_ja TEXT,
        error      TEXT,
        attempts   INTEGER NOT NULL DEFAULT 0,
        created_at TEXT,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transcript_segments (
        guid      TEXT NOT NULL,
        idx       INTEGER NOT NULL,
        start_sec REAL NOT NULL,
        end_sec   REAL NOT NULL,
        text_en   TEXT NOT NULL,
        text_ja   TEXT,
        PRIMARY KEY (guid, idx)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vocab (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        guid        TEXT NOT NULL,
        term        TEXT NOT NULL,
        kind        TEXT NOT NULL DEFAULT 'word' CHECK(kind IN ('word','phrase')),
        meaning_ja  TEXT NOT NULL,
        example_en  TEXT,
        example_ja  TEXT,
        segment_idx INTEGER,
        box         INTEGER NOT NULL DEFAULT 0,
        next_review TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','learning','known')),
        created_at  TEXT,
        UNIQUE (guid, term)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_vocab_review ON vocab(status, next_review)",
]

# 正解するたびに箱が1つ進み、次の出題までの日数が延びる（箱1→1日後 … 箱6→30日後）
REVIEW_INTERVALS = [1, 2, 4, 7, 15, 30]
MAX_BOX = len(REVIEW_INTERVALS)
KNOWN_BOX = 5  # この箱まで来たら「覚えた」扱い
MAX_ATTEMPTS = 3  # 文字起こしに失敗したエピソードを再挑戦する上限


def init_study_schema(conn: sqlite3.Connection) -> None:
    for sql in STUDY_SCHEMA:
        conn.execute(sql)


def _today(today: date | None) -> date:
    return today or date.today()


# ---------------------------------------------------------------------------
# 対象選び
# ---------------------------------------------------------------------------

def transcript_targets(conn: sqlite3.Connection) -> list[dict]:
    """文字起こしがまだ（または失敗して再挑戦できる）エピソードを新しい順に返す。"""
    rows = fetch_all(
        conn,
        "SELECT e.* FROM episodes e LEFT JOIN transcripts t ON t.guid = e.guid"
        " WHERE t.guid IS NULL OR (t.status = 'failed' AND t.attempts < ?)"
        " ORDER BY e.published DESC",
        (MAX_ATTEMPTS,),
    )
    return [dict(r) for r in rows]


def ai_targets(conn: sqlite3.Connection) -> list[dict]:
    """文字起こし済みで、和訳・単語抽出がまだ（または失敗した）エピソード。"""
    rows = fetch_all(
        conn,
        "SELECT e.* FROM episodes e JOIN transcripts t ON t.guid = e.guid"
        " WHERE t.status = 'done' AND t.ai_status IN ('none','failed','skipped')"
        " ORDER BY e.published DESC",
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 文字起こし
# ---------------------------------------------------------------------------

def save_transcript(conn: sqlite3.Connection, guid: str, segments: list[dict], model: str) -> None:
    """英文の文字起こしを保存する（以前の文と和訳・単語は置き換える）。"""
    now = now_str()
    conn.execute("DELETE FROM transcript_segments WHERE guid = ?", (guid,))
    conn.executemany(
        "INSERT INTO transcript_segments (guid, idx, start_sec, end_sec, text_en) VALUES (?,?,?,?,?)",
        [(guid, i, float(s["start"]), float(s["end"]), s["text"]) for i, s in enumerate(segments)],
    )
    conn.execute(
        "INSERT INTO transcripts (guid, status, ai_status, model, attempts, created_at, updated_at)"
        " VALUES (?, 'done', 'none', ?, 1, ?, ?)"
        " ON CONFLICT(guid) DO UPDATE SET status = 'done', ai_status = 'none', model = excluded.model,"
        " error = NULL, summary_ja = NULL, attempts = transcripts.attempts + 1, updated_at = excluded.updated_at",
        (guid, model, now, now),
    )


def mark_transcript_failed(conn: sqlite3.Connection, guid: str, error: str) -> None:
    now = now_str()
    conn.execute(
        "INSERT INTO transcripts (guid, status, error, attempts, created_at, updated_at)"
        " VALUES (?, 'failed', ?, 1, ?, ?)"
        " ON CONFLICT(guid) DO UPDATE SET status = 'failed', error = excluded.error,"
        " attempts = transcripts.attempts + 1, updated_at = excluded.updated_at",
        (guid, error[:500], now, now),
    )


def reset_attempts(conn: sqlite3.Connection, guid: str) -> None:
    """手動でやり直すときに失敗回数を戻す。"""
    conn.execute("UPDATE transcripts SET attempts = 0 WHERE guid = ? AND status = 'failed'", (guid,))


def get_segments(conn: sqlite3.Connection, guid: str) -> list[dict]:
    rows = fetch_all(conn, "SELECT * FROM transcript_segments WHERE guid = ? ORDER BY idx", (guid,))
    return [dict(r) for r in rows]


def save_translations(conn: sqlite3.Connection, guid: str, translations: dict[int, str]) -> None:
    conn.executemany(
        "UPDATE transcript_segments SET text_ja = ? WHERE guid = ? AND idx = ?",
        [(ja, guid, idx) for idx, ja in translations.items()],
    )


def save_study(conn: sqlite3.Connection, guid: str, summary_ja: str, vocab: list[dict],
               today: date | None = None) -> int:
    """要約と単語を保存して ai_status を done にする。追加した単語数を返す（重複は無視）。"""
    created = now_str()
    due = _today(today).isoformat()
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO vocab (guid, term, kind, meaning_ja, example_en, example_ja, segment_idx,"
        " next_review, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        [(guid, v["term"], v["kind"], v["meaning_ja"], v.get("example_en"), v.get("example_ja"),
          v.get("segment_idx"), due, created) for v in vocab],
    )
    added = conn.total_changes - before
    conn.execute(
        "UPDATE transcripts SET summary_ja = ?, ai_status = 'done', error = NULL, updated_at = ? WHERE guid = ?",
        (summary_ja, created, guid),
    )
    return added


def set_ai_status(conn: sqlite3.Connection, guid: str, status: str, error: str | None = None) -> None:
    conn.execute(
        "UPDATE transcripts SET ai_status = ?, error = ?, updated_at = ? WHERE guid = ?",
        (status, (error or "")[:500] or None, now_str(), guid),
    )


def get_transcript(conn: sqlite3.Connection, guid: str) -> dict | None:
    head = fetch_one(conn, "SELECT * FROM transcripts WHERE guid = ?", (guid,))
    if not head:
        return None
    return {**dict(head), "segments": get_segments(conn, guid), "vocab": list_vocab(conn, guid=guid)}


def list_study_episodes(conn: sqlite3.Connection) -> list[dict]:
    """全エピソードと文字起こしの状態（未作成は status=None）。新しい順。"""
    rows = fetch_all(
        conn,
        "SELECT e.*, s.art_file AS show_art, s.color AS show_color,"
        " t.status AS t_status, t.ai_status, t.error AS t_error, t.attempts,"
        " (SELECT COUNT(*) FROM vocab v WHERE v.guid = e.guid) AS vocab_count"
        " FROM episodes e LEFT JOIN shows s ON s.name = e.show"
        " LEFT JOIN transcripts t ON t.guid = e.guid ORDER BY e.published DESC",
    )
    return [dict(r) for r in rows]


def transcribed_guids(conn: sqlite3.Connection) -> set[str]:
    return {r["guid"] for r in fetch_all(conn, "SELECT guid FROM transcripts WHERE status = 'done'")}


# ---------------------------------------------------------------------------
# 単語帳
# ---------------------------------------------------------------------------

def list_vocab(conn: sqlite3.Connection, *, guid: str | None = None, status: str | None = None,
               show: str | None = None, due_only: bool = False, today: date | None = None) -> list[dict]:
    sql = (
        "SELECT v.*, e.show, e.title AS episode_title FROM vocab v"
        " LEFT JOIN episodes e ON e.guid = v.guid WHERE 1=1"
    )
    params: list = []
    if guid:
        sql += " AND v.guid = ?"
        params.append(guid)
    if status:
        sql += " AND v.status = ?"
        params.append(status)
    if show:
        sql += " AND e.show = ?"
        params.append(show)
    if due_only:
        sql += " AND v.status != 'known' AND v.next_review <= ?"
        params.append(_today(today).isoformat())
    sql += " ORDER BY v.segment_idx, v.id" if guid else " ORDER BY v.next_review, v.id DESC"
    return [dict(r) for r in fetch_all(conn, sql, tuple(params))]


def get_vocab(conn: sqlite3.Connection, vocab_id: int) -> dict | None:
    row = fetch_one(conn, "SELECT * FROM vocab WHERE id = ?", (vocab_id,))
    return dict(row) if row else None


def next_box(box: int, ok: bool, today: date) -> tuple[int, str, str]:
    """復習結果から (新しい箱, 次の出題日, 状態) を決める。"""
    if not ok:
        return 0, today.isoformat(), "learning"  # 間違えたら最初から。今日のうちにもう一度
    new_box = min(box + 1, MAX_BOX)
    due = today + timedelta(days=REVIEW_INTERVALS[new_box - 1])
    return new_box, due.isoformat(), "known" if new_box >= KNOWN_BOX else "learning"


def review_vocab(conn: sqlite3.Connection, vocab_id: int, ok: bool, today: date | None = None) -> dict | None:
    current = get_vocab(conn, vocab_id)
    if not current:
        return None
    box, due, status = next_box(current["box"], ok, _today(today))
    conn.execute("UPDATE vocab SET box = ?, next_review = ?, status = ? WHERE id = ?", (box, due, status, vocab_id))
    return get_vocab(conn, vocab_id)


def set_vocab_known(conn: sqlite3.Connection, vocab_id: int, known: bool, today: date | None = None) -> dict | None:
    """「覚えた」ボタン。取り消すと今日から復習し直す。"""
    if not get_vocab(conn, vocab_id):
        return None
    day = _today(today)
    if known:
        values = (MAX_BOX, (day + timedelta(days=REVIEW_INTERVALS[-1])).isoformat(), "known")
    else:
        values = (0, day.isoformat(), "learning")
    conn.execute("UPDATE vocab SET box = ?, next_review = ?, status = ? WHERE id = ?", (*values, vocab_id))
    return get_vocab(conn, vocab_id)


def study_stats(conn: sqlite3.Connection, today: date | None = None) -> dict:
    day = _today(today).isoformat()
    ready = fetch_one(conn, "SELECT COUNT(*) AS c FROM transcripts WHERE status = 'done'")["c"]
    vocab = fetch_one(
        conn,
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN status = 'known' THEN 1 ELSE 0 END) AS known,"
        " SUM(CASE WHEN status != 'known' AND next_review <= ? THEN 1 ELSE 0 END) AS due FROM vocab",
        (day,),
    )
    return {"ready": ready, "vocab": vocab["total"], "known": vocab["known"] or 0, "due": vocab["due"] or 0}
