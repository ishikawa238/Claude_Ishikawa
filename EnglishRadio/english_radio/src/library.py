"""
mp3 ライブラリ管理。

新しいエピソードだけをダウンロードして library/<番組>/ に保存し、DB に登録する。
ダウンロードした mp3 は削除しない（貯まった分はシャッフル再生に使う）。
"""

import re
import sqlite3
from pathlib import Path

import requests

from shared.logger import get_logger
from src import store
from src.feeds import USER_AGENT, Episode

DOWNLOAD_TIMEOUT_SEC = (15, 120)  # (接続, 読み取り)
DOWNLOAD_ATTEMPTS = 2
CHUNK_SIZE = 1024 * 256
MAX_NAME_LEN = 80
MAX_DOWNLOAD_BYTES = 600 * 1024 * 1024  # 1話 600MB を超えたら異常とみなす
_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_RESERVED_NAMES = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.IGNORECASE)


def _logger():
    return get_logger(__name__)


def safe_name(text: str, max_len: int = MAX_NAME_LEN) -> str:
    """Windows のファイル名に使えない文字を除き、長さを制限する。"""
    cleaned = _INVALID_CHARS.sub("", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".")
    cleaned = cleaned[:max_len].rstrip(" .") or "untitled"
    # CON / NUL / COM1 などは Windows の予約名でファイル名に使えない
    if _RESERVED_NAMES.match(cleaned.split(".")[0]):
        cleaned = f"_{cleaned}"
    return cleaned


def episode_path(library_dir: Path, ep: Episode) -> Path:
    date_part = (ep.published[:10] or "unknown").replace("-", "")
    base = f"{date_part}_{safe_name(ep.title)}"
    folder = library_dir / safe_name(ep.show, 40)
    path = folder / f"{base}.mp3"
    counter = 2
    while path.exists():
        path = folder / f"{base}_{counter}.mp3"
        counter += 1
    return path


def download_file(url: str, dest: Path, session: requests.Session | None = None) -> int:
    """url を dest に保存し、バイト数を返す。途中で失敗したら .part を消す。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    http = session or requests
    try:
        with http.get(
            url, stream=True, timeout=DOWNLOAD_TIMEOUT_SEC, headers={"User-Agent": USER_AGENT}
        ) as resp:
            resp.raise_for_status()
            size = 0
            with open(part, "wb") as f:
                for chunk in resp.iter_content(CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
                        size += len(chunk)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise IOError("ファイルが大きすぎるため中止しました")
        if size == 0:
            raise IOError("ダウンロードしたファイルが空です")
        part.replace(dest)
        return size
    finally:
        if part.exists():
            part.unlink()


def read_duration(path: Path) -> int | None:
    """mp3 から再生時間（秒）を読む。読めなければ None。"""
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(str(path))
        if audio is not None and audio.info and audio.info.length:
            return int(audio.info.length)
    except Exception as e:
        _logger().warning(f"再生時間を読めませんでした: {path.name} ({e})")
    return None


def _download_with_retry(
    ep: Episode, dest: Path, session: requests.Session | None, attempts: int = DOWNLOAD_ATTEMPTS
) -> int | None:
    """一時的な失敗に備えて数回試す。全部失敗したら None（次回の実行で再挑戦される）。"""
    for attempt in range(1, attempts + 1):
        try:
            return download_file(ep.audio_url, dest, session)
        except Exception as e:
            _logger().warning(f"{ep.show}: ダウンロード失敗 '{ep.title}' ({attempt}/{attempts}回目: {e})")
    return None


def download_new(
    conn: sqlite3.Connection,
    episodes: list[Episode],
    library_dir: Path,
    session: requests.Session | None = None,
) -> list[dict]:
    """未取得のエピソードだけをダウンロードし、登録したエントリを返す。"""
    known = store.known_guids(conn)
    added: list[dict] = []
    for ep in episodes:
        if ep.guid in known:
            continue
        dest = episode_path(library_dir, ep)
        size = _download_with_retry(ep, dest, session)
        if size is None:
            continue
        entry = {
            "guid": ep.guid,
            "show": ep.show,
            "title": ep.title,
            "audio_url": ep.audio_url,
            "path": str(dest),
            "published": ep.published,
            "duration_sec": ep.duration_sec or read_duration(dest),
            "size_bytes": size,
        }
        store.add_episode(conn, entry)
        known.add(ep.guid)
        added.append(store.get_episode(conn, ep.guid))
        _logger().info(f"{ep.show}: 保存 '{ep.title}' ({size / 1_048_576:.1f}MB)")
    return added
