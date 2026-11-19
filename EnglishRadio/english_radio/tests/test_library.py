import unittest
from pathlib import Path
from unittest import mock

from src import library, store
from src.feeds import Episode
from tests.helpers import TempEnvTestCase


def episode(guid: str, title: str = "Hello: World?", show: str = "Show/A") -> Episode:
    return Episode(show=show, title=title, guid=guid, audio_url=f"https://x/{guid}.mp3",
                   published="2026-11-19 07:00:00", duration_sec=600)


class FakeResponse:
    def __init__(self, chunks, fail_midway=False):
        self.chunks = chunks
        self.fail_midway = fail_midway

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        for chunk in self.chunks:
            yield chunk
        if self.fail_midway:
            raise ConnectionError("cut")


class SafeNameTest(unittest.TestCase):
    def test_removes_invalid_chars(self):
        self.assertEqual(library.safe_name('a<b>c:d"e/f\\g|h?i*j'), "abcdefghij")

    def test_length_and_empty(self):
        self.assertEqual(len(library.safe_name("x" * 200)), library.MAX_NAME_LEN)
        self.assertEqual(library.safe_name("???"), "untitled")
        self.assertEqual(library.safe_name("  dots... "), "dots")

    def test_reserved_windows_names(self):
        self.assertEqual(library.safe_name("CON"), "_CON")
        self.assertEqual(library.safe_name("com1"), "_com1")
        self.assertEqual(library.safe_name("Console"), "Console")


class DownloadNewTest(TempEnvTestCase):
    def test_downloads_only_unknown_and_registers(self):
        with self.conn() as conn:
            store.add_episode(conn, {"guid": "known", "show": "S", "title": "T", "path": "p"})
        session = mock.Mock()
        session.get.return_value = FakeResponse([b"abc", b"def"])
        with self.conn() as conn:
            added = library.download_new(conn, [episode("known"), episode("new1")], self.root / "library", session)
        self.assertEqual([e["guid"] for e in added], ["new1"])
        self.assertEqual(session.get.call_count, 1)
        path = Path(added[0]["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), b"abcdef")
        self.assertEqual(path.parent.name, "ShowA")
        self.assertEqual(path.name, "20261119_Hello World.mp3")
        self.assertEqual(added[0]["size_bytes"], 6)

    def test_failed_download_leaves_no_part_and_no_row(self):
        session = mock.Mock()
        session.get.side_effect = lambda *a, **k: FakeResponse([b"abc"], fail_midway=True)
        with self.conn() as conn:
            added = library.download_new(conn, [episode("bad")], self.root / "library", session)
            self.assertEqual(added, [])
            self.assertEqual(store.known_guids(conn), set())
        self.assertEqual(session.get.call_count, library.DOWNLOAD_ATTEMPTS)
        self.assertEqual(list((self.root / "library").rglob("*.part")), [])
        self.assertEqual(list((self.root / "library").rglob("*.mp3")), [])

    def test_same_title_gets_unique_name(self):
        session = mock.Mock()
        session.get.side_effect = lambda *a, **k: FakeResponse([b"x"])
        with self.conn() as conn:
            added = library.download_new(conn, [episode("g1"), episode("g2")], self.root / "library", session)
        names = sorted(Path(e["path"]).name for e in added)
        self.assertEqual(names, ["20261119_Hello World.mp3", "20261119_Hello World_2.mp3"])


if __name__ == "__main__":
    unittest.main()
