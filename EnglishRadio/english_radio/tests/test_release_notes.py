import tempfile
import unittest
from pathlib import Path

from src import release_notes


class ReleaseNotesTest(unittest.TestCase):
    def test_parse_multiple_versions(self):
        text = (
            "# リリースノート\n説明文\n\n"
            "## v1.1.0 — 2026-11-20 新機能\n- A を追加\n- B を修正\n\n"
            "## v1.0.0 - 2026-11-19 最初\n- 誕生\n"
        )
        notes = release_notes.parse(text)
        self.assertEqual([n["version"] for n in notes], ["v1.1.0", "v1.0.0"])
        self.assertEqual(notes[0]["date"], "2026-11-20")
        self.assertEqual(notes[0]["title"], "新機能")
        self.assertEqual(notes[0]["items"], ["A を追加", "B を修正"])
        self.assertEqual(notes[1]["items"], ["誕生"])

    def test_bullets_before_first_heading_and_bad_headings_are_ignored(self):
        text = "- 迷子の行\n## 版なし見出し\n- 迷子2\n## v2.0 — 2026-01-01\n- OK\n"
        notes = release_notes.parse(text)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["title"], "")
        self.assertEqual(notes[0]["items"], ["OK"])

    def test_empty_and_missing_file(self):
        self.assertEqual(release_notes.parse(""), [])
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(release_notes.load(Path(tmp) / "none.md"), [])
            self.assertIsNone(release_notes.latest(Path(tmp) / "none.md"))

    def test_bundled_notes_are_valid(self):
        notes = release_notes.load()
        self.assertTrue(notes, "RELEASE_NOTES.md が読めること")
        for note in notes:
            self.assertTrue(note["items"], f"{note['version']} に箇条書きがあること")


if __name__ == "__main__":
    unittest.main()
