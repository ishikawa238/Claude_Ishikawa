import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import player


class WriteM3uTest(unittest.TestCase):
    def test_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out" / "today.m3u"
            player.write_m3u([
                {"show": "BBC", "title": "日本語\nタイトル", "duration_sec": 60, "path": "C:/a.mp3"},
                {"show": "NPR", "title": "B", "duration_sec": None, "path": "C:/b.mp3"},
            ], path)
            lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, [
            "#EXTM3U",
            "#EXTINF:60,BBC - 日本語 タイトル",
            "C:/a.mp3",
            "#EXTINF:-1,NPR - B",
            "C:/b.mp3",
        ])


class PlayTest(unittest.TestCase):
    def test_launches_vlc_with_play_and_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            vlc = Path(tmp) / "vlc.exe"
            vlc.write_bytes(b"")
            with mock.patch.object(player.subprocess, "Popen") as popen:
                used = player.play(Path("C:/today.m3u"), str(vlc))
        self.assertEqual(used, str(vlc))
        args = popen.call_args[0][0]
        self.assertEqual(args[0], str(vlc))
        self.assertIn("--play-and-exit", args)
        self.assertEqual(args[-1], str(Path("C:/today.m3u")))

    def test_missing_vlc_raises(self):
        with mock.patch.object(player, "_DEFAULT_VLC_PATHS", []), \
             mock.patch.object(player.shutil, "which", return_value=None), \
             mock.patch.object(player.subprocess, "Popen") as popen:
            with self.assertRaises(player.VlcNotFoundError):
                player.play(Path("x.m3u"), "C:/nowhere/vlc.exe")
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
