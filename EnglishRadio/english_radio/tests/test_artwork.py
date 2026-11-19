import io
import sqlite3
import unittest
from unittest import mock

from PIL import Image

from shared.db import get_connection
from src import artwork, store
from tests.helpers import TempEnvTestCase


def image_bytes(size=(1200, 900), color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


class FakeResponse:
    def __init__(self, data: bytes):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        yield self.data


class CacheImageTest(TempEnvTestCase):
    URL = "https://example.com/cover.png"

    def test_resizes_to_square_jpeg_and_caches(self):
        session = mock.Mock()
        session.get.return_value = FakeResponse(image_bytes())
        name = artwork.cache_image(self.URL, session)
        self.assertRegex(name, artwork.ART_NAME_RE)
        with Image.open(artwork.art_dir() / name) as img:
            self.assertEqual(img.size, (artwork.ART_SIZE, artwork.ART_SIZE))
            self.assertEqual(img.format, "JPEG")
        self.assertEqual(artwork.cache_image(self.URL, session), name)
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(sorted(p.name for p in artwork.art_dir().iterdir()), [name])

    def test_broken_image_returns_none_and_leaves_nothing(self):
        session = mock.Mock()
        session.get.return_value = FakeResponse(b"not an image")
        self.assertIsNone(artwork.cache_image(self.URL, session))
        self.assertEqual(list(artwork.art_dir().iterdir()), [])

    def test_rejects_non_http(self):
        self.assertIsNone(artwork.cache_image("ftp://example.com/x.png"))
        self.assertIsNone(artwork.cache_image(""))

    def test_dominant_color_is_reddish(self):
        path = self.root / "red.png"
        Image.new("RGB", (50, 50), (200, 30, 30)).save(path)
        color = artwork.dominant_color(path)
        r, g = int(color[1:3], 16), int(color[3:5], 16)
        self.assertGreater(r, g + 100)

    def test_dominant_color_fallback(self):
        path = self.root / "gray.png"
        Image.new("RGB", (50, 50), (128, 128, 128)).save(path)
        self.assertEqual(artwork.dominant_color(path), artwork.FALLBACK_COLOR)
        self.assertEqual(artwork.dominant_color(self.root / "none.png"), artwork.FALLBACK_COLOR)


class MigrationTest(TempEnvTestCase):
    def test_old_schema_gets_new_columns_and_keeps_rows(self):
        old_db = self.root / "old.db"
        conn = sqlite3.connect(old_db)
        conn.execute(
            "CREATE TABLE episodes (guid TEXT PRIMARY KEY, show TEXT NOT NULL, title TEXT NOT NULL,"
            " audio_url TEXT, path TEXT NOT NULL, published TEXT, duration_sec INTEGER,"
            " size_bytes INTEGER DEFAULT 0, downloaded_at TEXT, last_played TEXT,"
            " play_count INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute("INSERT INTO episodes (guid, show, title, path) VALUES ('g', 'S', 'T', 'p')")
        conn.commit()
        conn.close()

        with get_connection(old_db) as conn:
            store.init_db(conn)
            store.init_db(conn)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(episodes)")}
            ep = store.get_episode(conn, "g")
        self.assertTrue({"image_url", "art_file", "summary"} <= cols)
        self.assertEqual(ep["title"], "T")
        self.assertIsNone(ep["show_art"])

    def test_show_art_joined_to_episodes(self):
        self.add_episode_file("a", show="A")
        art = "f" * 40 + ".jpg"
        with self.conn() as conn:
            store.upsert_show(conn, "A", "https://x/a.jpg", art, "#112233")
            store.upsert_show(conn, "A", "https://x/a.jpg", None, None)
            ep = store.get_episode(conn, "a")
            stats = store.library_stats(conn)
        self.assertEqual(ep["show_art"], art)
        self.assertEqual(ep["show_color"], "#112233")
        self.assertEqual(stats["by_show"][0]["show_art"], art)


if __name__ == "__main__":
    unittest.main()
