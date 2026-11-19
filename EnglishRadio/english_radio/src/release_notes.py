"""
リリースノート（RELEASE_NOTES.md）の読み込み。

形式:
    ## v1.2.0 — 2026-11-19 タイトル
    - 箇条書き
見出しと箇条書き以外の行は無視する。
"""

import re
from pathlib import Path

from src import paths

NOTES_FILE = paths.PROJECT_ROOT / "RELEASE_NOTES.md"

_HEADING_RE = re.compile(r"^##\s+(v[\w.\-]+)\s*[—–-]\s*(\d{4}-\d{2}-\d{2})\s*(.*)$")


def parse(text: str) -> list[dict]:
    """Markdown を [{version, date, title, items}] に変換する（ファイルの順のまま）。"""
    notes: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        heading = _HEADING_RE.match(line)
        if heading:
            version, date, title = heading.groups()
            notes.append({"version": version, "date": date, "title": title.strip(), "items": []})
        elif line.startswith("- ") and notes:
            notes[-1]["items"].append(line[2:].strip())
    return notes


def load(path: Path | None = None) -> list[dict]:
    """リリースノートを読み込む。ファイルが無ければ空リスト。"""
    path = path or NOTES_FILE
    if not path.is_file():
        return []
    return parse(path.read_text(encoding="utf-8"))


def latest(path: Path | None = None) -> dict | None:
    notes = load(path)
    return notes[0] if notes else None
