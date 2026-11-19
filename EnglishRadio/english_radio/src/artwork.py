"""
番組・エピソードのアートワーク（ジャケット画像）のキャッシュ。

RSS の画像は 3000px 級で重いので、600px の正方形 JPEG に縮小して
data/artwork/<sha1(url)>.jpg に保存する。Web 画面はこのローカル画像だけを表示する
（外部への直リンクをしないので、オフラインでも表示でき、ページも軽い）。
失敗しても朝の再生は止めない（None を返して WARNING を出すだけ）。
"""

import hashlib
import re
from collections import Counter
from pathlib import Path

import requests
from PIL import Image, ImageOps

from shared.logger import get_logger
from src import paths
from src.library import download_file

ART_SIZE = 600
JPEG_QUALITY = 85
MAX_IMAGE_BYTES = 20 * 1024 * 1024
ART_NAME_RE = re.compile(r"^[0-9a-f]{40}\.jpg$")
FALLBACK_COLOR = "#3a3a48"


def _logger():
    return get_logger(__name__)


def art_dir() -> Path:
    return paths.data_dir() / "artwork"


def art_name(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest() + ".jpg"


def cache_image(url: str, session: requests.Session | None = None) -> str | None:
    """画像を縮小してキャッシュし、ファイル名を返す。既にあれば取得しない。"""
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    name = art_name(url)
    dest = art_dir() / name
    if dest.exists():
        return name
    raw = dest.with_suffix(".src")
    try:
        download_file(url, raw, session, max_bytes=MAX_IMAGE_BYTES)
        with Image.open(raw) as img:
            thumb = ImageOps.fit(img.convert("RGB"), (ART_SIZE, ART_SIZE), Image.Resampling.LANCZOS)
        tmp = dest.with_suffix(".tmp")
        thumb.save(tmp, "JPEG", quality=JPEG_QUALITY, optimize=True)
        tmp.replace(dest)
        return name
    except Exception as e:
        _logger().warning(f"アートワークを取得できませんでした: {url[:80]} ({e})")
        return None
    finally:
        for leftover in (raw, dest.with_suffix(".tmp")):
            if leftover.exists():
                leftover.unlink()


def dominant_color(path: Path) -> str:
    """画像の代表色（暗すぎ・白っぽすぎる色を除いた最頻色）を "#rrggbb" で返す。"""
    try:
        with Image.open(path) as img:
            small = img.convert("RGB").resize((32, 32))
            quantized = small.quantize(colors=8).convert("RGB")
            pixels = list(quantized.get_flattened_data())
    except Exception:
        return FALLBACK_COLOR
    counts = Counter(pixels)
    for (r, g, b), _ in counts.most_common():
        brightness = (r * 299 + g * 587 + b * 114) / 1000
        if 40 <= brightness <= 220 and max(r, g, b) - min(r, g, b) > 25:
            return f"#{r:02x}{g:02x}{b:02x}"
    return FALLBACK_COLOR
