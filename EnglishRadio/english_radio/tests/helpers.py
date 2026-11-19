"""テスト用の一時環境（config / DB / ライブラリ / ログを一時フォルダに作る）。"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from shared.config import init_config
from shared.db import get_connection
from src import store


def make_entry(guid: str, show: str = "Show A", duration: int = 600, published: str = "2026-11-19 07:00:00",
               last_played: str | None = None, path: str = "") -> dict:
    return {
        "guid": guid, "show": show, "title": f"Title {guid}", "audio_url": f"https://x/{guid}.mp3",
        "path": path or f"C:/lib/{guid}.mp3", "published": published, "duration_sec": duration,
        "size_bytes": 1000, "last_played": last_played, "play_count": 1 if last_played else 0,
    }


class TempEnvTestCase(unittest.TestCase):
    """一時フォルダに config.json を作り、init_config する。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        os.environ["SHARED_LOG_DIR"] = str(self.root / "logs")
        self.config_path = self.root / "config.json"
        self.config = {
            "radio": {
                "target_minutes": 30,
                "episodes_per_feed": 3,
                "vlc_path": str(self.root / "no_vlc.exe"),
                "schedules": [{"label": "Morning", "start": "07:00", "end": "07:30", "days": "daily"}],
                "feeds": [{"name": "Show A", "url": "https://example.com/a.rss", "enabled": True}],
            },
            "paths": {
                "db_path": str(self.root / "data" / "test.db"),
                "library_dir": str(self.root / "library"),
                "data_dir": str(self.root / "data"),
            },
            "slack": {"webhook_url": ""},
            "logging": {"log_level": "WARNING", "log_dir": str(self.root / "logs")},
        }
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        init_config(self.config_path)
        (self.root / "data").mkdir()
        (self.root / "library").mkdir()
        self.db_path = self.root / "data" / "test.db"
        with get_connection(self.db_path) as conn:
            store.init_db(conn)

    def tearDown(self) -> None:
        import logging

        # Windows ではログファイルを閉じないと一時フォルダを消せない。
        # コンソール用ハンドラは sys.stdout.buffer を包んでいるので閉じない（閉じると stdout ごと閉じる）。
        for name in list(logging.Logger.manager.loggerDict):
            logger = logging.getLogger(name)
            for handler in list(logger.handlers):
                if isinstance(handler, logging.FileHandler):
                    handler.close()
                    logger.removeHandler(handler)
        os.environ.pop("SHARED_LOG_DIR", None)
        self._tmp.cleanup()

    def conn(self):
        return get_connection(self.db_path)

    def add_episode_file(self, guid: str, **kwargs) -> dict:
        """ライブラリに実ファイルを作り、DB にも登録する。"""
        path = self.root / "library" / f"{guid}.mp3"
        path.write_bytes(b"ID3fake")
        entry = make_entry(guid, path=str(path), **kwargs)
        with self.conn() as conn:
            store.add_episode(conn, entry)
        return entry
