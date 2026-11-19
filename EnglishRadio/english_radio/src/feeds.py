"""
ポッドキャスト RSS の取得とエピソード抽出。

認証不要の公開 RSS から、各番組の新しい順にエピソードを取り出す。
1番組の失敗は他の番組に影響させない（WARNING を出してスキップ）。
"""

from dataclasses import dataclass
from datetime import datetime

import feedparser
import requests

from shared.logger import get_logger

USER_AGENT = "EnglishRadio/1.0 (personal podcast player)"
FETCH_TIMEOUT_SEC = 30


def _logger():
    return get_logger(__name__)


@dataclass(frozen=True)
class Episode:
    show: str
    title: str
    guid: str
    audio_url: str
    published: str  # "YYYY-MM-DD HH:MM:SS"（不明なら空文字）
    duration_sec: int | None


def parse_duration(value) -> int | None:
    """itunes:duration（"HH:MM:SS" / "MM:SS" / 秒数）を秒に変換する。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parts = [int(float(p)) for p in text.split(":")]
    except ValueError:
        return None
    if len(parts) > 3:
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds if seconds > 0 else None


def _audio_url(entry) -> str | None:
    for enc in entry.get("enclosures", []):
        href = enc.get("href") or enc.get("url")
        if not href:
            continue
        mime = enc.get("type", "")
        if mime.startswith("audio") or href.split("?")[0].lower().endswith(".mp3"):
            return href
    return None


def _published(entry) -> str:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return ""
    return datetime(*parsed[:6]).strftime("%Y-%m-%d %H:%M:%S")


def parse_feed(content: bytes | str, show_name: str, limit: int) -> tuple[str, list[Episode]]:
    """RSS 本文を解析し、(番組タイトル, 新しい順のエピソード) を返す。"""
    parsed = feedparser.parse(content)
    feed_title = parsed.feed.get("title", "") or show_name
    if not parsed.entries and parsed.bozo:
        raise ValueError(f"RSS を解析できません: {parsed.get('bozo_exception')}")

    episodes = []
    for entry in parsed.entries:
        audio = _audio_url(entry)
        if not audio:
            continue
        episodes.append(Episode(
            show=show_name or feed_title,
            title=entry.get("title", "(無題)").strip(),
            guid=entry.get("id") or audio,
            audio_url=audio,
            published=_published(entry),
            duration_sec=parse_duration(entry.get("itunes_duration")),
        ))

    episodes.sort(key=lambda e: e.published, reverse=True)
    return feed_title, episodes[:limit]


def download_feed(url: str, session: requests.Session | None = None) -> bytes:
    """RSS 本文を取得する。http/https 以外は拒否する。"""
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("RSS の URL は http:// か https:// で始めてください")
    http = session or requests
    resp = http.get(url, timeout=FETCH_TIMEOUT_SEC, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp.content


def fetch_feed(feed: dict, limit: int, session: requests.Session | None = None) -> list[Episode]:
    """config の feed 1件分を取得する。"""
    content = download_feed(feed["url"], session)
    _, episodes = parse_feed(content, feed.get("name", ""), limit)
    return episodes


def fetch_all(feeds: list[dict], limit: int, session: requests.Session | None = None) -> list[Episode]:
    """有効な全番組を取得する。失敗した番組はスキップする。"""
    episodes: list[Episode] = []
    for feed in feeds:
        if not feed.get("enabled", True):
            continue
        name = feed.get("name", feed.get("url", "?"))
        try:
            items = fetch_feed(feed, limit, session)
            episodes.extend(items)
            _logger().info(f"{name}: {len(items)} エピソード取得")
        except Exception as e:
            _logger().warning(f"{name}: 取得失敗 ({e})")
    return episodes
