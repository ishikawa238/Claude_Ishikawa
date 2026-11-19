"""
config.json の安全な書き換え（Web 管理画面用）。

検証 → 一時ファイルに書く → os.replace で置き換える、の順で
途中で落ちても壊れた JSON が残らないようにする。
"""

import json
import os
import tempfile
import threading
from pathlib import Path

from shared.config import init_config

MAX_EPISODES_PER_FEED = 10
_write_lock = threading.Lock()


def load_raw(config_path: Path) -> dict:
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def save_atomic(config_path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=config_path.parent, prefix=".config_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, config_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def update_radio(config_path: Path, changes: dict) -> dict:
    """radio セクションの複数キーを1回で書き換えて保存し、メモリ上の設定も更新する。

    Web サーバーはマルチスレッドなので、読み書きをロックで直列化して更新の取りこぼしを防ぐ。
    """
    with _write_lock:
        data = load_raw(config_path)
        data.setdefault("radio", {}).update(changes)
        save_atomic(config_path, data)
        return init_config(config_path)


def validate_feeds(raw: list) -> list[dict]:
    """番組リストを検証して整形する。不正なら ValueError。"""
    if not isinstance(raw, list):
        raise ValueError("番組リストの形式が正しくありません")
    cleaned = []
    urls = set()
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"{i}件目の形式が正しくありません")
        name = str(item.get("name", "")).strip()[:60]
        url = str(item.get("url", "")).strip()
        if not name:
            raise ValueError(f"{i}件目: 番組名を入力してください")
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError(f"{name}: RSS の URL は http:// か https:// で始めてください")
        if url in urls:
            raise ValueError(f"{name}: 同じ URL がすでに登録されています")
        urls.add(url)
        cleaned.append({"name": name, "url": url, "enabled": bool(item.get("enabled", True))})
    return cleaned


def validate_episodes_per_feed(value) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        raise ValueError("1回に取る本数は数字で入力してください") from None
    if not 1 <= num <= MAX_EPISODES_PER_FEED:
        raise ValueError(f"1回に取る本数は 1〜{MAX_EPISODES_PER_FEED} にしてください")
    return num
