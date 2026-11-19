"""
ポッドキャスト RSS の取得とエピソード抽出。

認証不要の公開 RSS から、各番組の新しい順にエピソードを取り出す。
1番組の失敗は他の番組に影響させない（WARNING を出してスキップ）。
"""

import html
import re
from dataclasses import dataclass, field
from datetime import datetime

import feedparser
import requests

from shared.logger import get_logger

USER_AGENT = "EnglishRadio/1.0 (personal podcast player)"
FETCH_TIMEOUT_SEC = 30
SUMMARY_MAX_LEN = 300
_TAG_RE = re.compile(r"<[^>]+>")


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
    image_url: str = ""  # エピソード画像。なければ番組画像
    summary: str = ""


@dataclass(frozen=True)
class FeedResult:
    title: str
    image_url: str
    episodes: list[Episode] = field(default_factory=list)


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


def _image(obj) -> str:
    img = obj.get("image") or {}
    return img.get("href") or img.get("url") or ""


def clean_summary(text: str, max_len: int = SUMMARY_MAX_LEN) -> str:
    """HTML タグを除き、空白を詰めて max_len 文字に切る。"""
    plain = html.unescape(_TAG_RE.sub(" ", text or ""))
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain if len(plain) <= max_len else plain[: max_len - 1].rstrip() + "…"


def parse_feed_result(content: bytes | str, show_name: str, limit: int) -> FeedResult:
    """RSS 本文を解析し、番組タイトル・番組画像・新しい順のエピソードを返す。"""
    parsed = feedparser.parse(content)
    feed_title = parsed.feed.get("title", "") or show_name
    feed_image = _image(parsed.feed)
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
            image_url=_image(entry) or feed_image,
            summary=clean_summary(entry.get("summary", "")),
        ))

    episodes.sort(key=lambda e: e.published, reverse=True)
    return FeedResult(title=feed_title, image_url=feed_image, episodes=episodes[:limit])


def parse_feed(content: bytes | str, show_name: str, limit: int) -> tuple[str, list[Episode]]:
    """(番組タイトル, 新しい順のエピソード) を返す（互換用）。"""
    result = parse_feed_result(content, show_name, limit)
    return result.title, result.episodes


def download_feed(url: str, session: requests.Session | None = None) -> bytes:
    """RSS 本文を取得する。http/https 以外は拒否する。"""
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("RSS の URL は http:// か https:// で始めてください")
    http = session or requests
    resp = http.get(url, timeout=FETCH_TIMEOUT_SEC, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp.content


def fetch_feed(feed: dict, limit: int, session: requests.Session | None = None) -> FeedResult:
    """config の feed 1件分を取得する。"""
    content = download_feed(feed["url"], session)
    return parse_feed_result(content, feed.get("name", ""), limit)


def fetch_all(
    feeds: list[dict], limit: int, session: requests.Session | None = None
) -> tuple[list[Episode], dict[str, str]]:
    """有効な全番組を取得し、(エピソード, {番組名: 番組画像URL}) を返す。失敗した番組はスキップ。"""
    episodes: list[Episode] = []
    show_images: dict[str, str] = {}
    for feed in feeds:
        if not feed.get("enabled", True):
            continue
        name = feed.get("name", feed.get("url", "?"))
        try:
            result = fetch_feed(feed, limit, session)
        except Exception as e:
            _logger().warning(f"{name}: 取得失敗 ({e})")
            continue
        episodes.extend(result.episodes)
        if result.image_url:
            show_images[name] = result.image_url
        _logger().info(f"{name}: {len(result.episodes)} エピソード取得")
    return episodes, show_images
