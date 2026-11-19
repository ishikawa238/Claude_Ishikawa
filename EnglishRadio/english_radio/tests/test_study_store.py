import unittest
from datetime import date

from shared.batch import start_batch_log
from src import store
from src.study import store as study_store
from tests.helpers import TempEnvTestCase

TODAY = date(2026, 11, 20)
SEGMENTS = [
    {"start": 0.0, "end": 2.5, "text": "Hello and welcome."},
    {"start": 2.5, "end": 6.0, "text": "Today we talk about microplastics."},
]
VOCAB = [
    {"term": "microplastics", "kind": "word", "meaning_ja": "マイクロプラスチック",
     "example_en": "Today we talk about microplastics.", "example_ja": "今日はマイクロプラスチックの話です。",
     "segment_idx": 1},
    {"term": "talk about", "kind": "phrase", "meaning_ja": "〜について話す", "segment_idx": 1},
]


class StudyStoreTest(TempEnvTestCase):
    def setUp(self):
        super().setUp()
        self.add_episode_file("old", published="2026-11-01 07:00:00")
        self.add_episode_file("new", published="2026-11-19 07:00:00")

    def test_targets_newest_first_and_failed_retry_limit(self):
        with self.conn() as conn:
            self.assertEqual([e["guid"] for e in study_store.transcript_targets(conn)], ["new", "old"])
            study_store.save_transcript(conn, "new", SEGMENTS, "small.en")
            for _ in range(study_store.MAX_ATTEMPTS):
                study_store.mark_transcript_failed(conn, "old", "boom")
            self.assertEqual(study_store.transcript_targets(conn), [])
            study_store.reset_attempts(conn, "old")
            self.assertEqual([e["guid"] for e in study_store.transcript_targets(conn)], ["old"])
            self.assertEqual([e["guid"] for e in study_store.ai_targets(conn)], ["new"])

    def test_save_and_get_transcript_with_translations_and_vocab(self):
        with self.conn() as conn:
            study_store.save_transcript(conn, "new", SEGMENTS, "small.en")
            study_store.save_translations(conn, "new", {0: "こんにちは。", 1: "今日はマイクロプラスチックの話です。"})
            added = study_store.save_study(conn, "new", "要約です", VOCAB, today=TODAY)
            again = study_store.save_study(conn, "new", "要約です", VOCAB, today=TODAY)
            t = study_store.get_transcript(conn, "new")
        self.assertEqual((added, again), (2, 0))  # 同じ単語は重複しない
        self.assertEqual(t["ai_status"], "done")
        self.assertEqual(t["summary_ja"], "要約です")
        self.assertEqual([s["text_ja"] for s in t["segments"]], ["こんにちは。", "今日はマイクロプラスチックの話です。"])
        self.assertEqual([v["term"] for v in t["vocab"]], ["microplastics", "talk about"])
        self.assertEqual(t["vocab"][0]["next_review"], "2026-11-20")

    def test_retranscribe_replaces_segments_and_resets_ai(self):
        with self.conn() as conn:
            study_store.save_transcript(conn, "new", SEGMENTS, "small.en")
            study_store.save_study(conn, "new", "要約", [], today=TODAY)
            study_store.save_transcript(conn, "new", SEGMENTS[:1], "base.en")
            t = study_store.get_transcript(conn, "new")
        self.assertEqual(len(t["segments"]), 1)
        self.assertEqual((t["ai_status"], t["model"], t["summary_ja"]), ("none", "base.en", None))

    def test_leitner_intervals(self):
        box, due, status = 0, None, None
        expected = ["2026-11-21", "2026-11-22", "2026-11-24", "2026-11-27", "2026-12-05", "2026-12-20"]
        for want in expected:
            box, due, status = study_store.next_box(box, True, TODAY)
            self.assertEqual(due, want)
        self.assertEqual((box, status), (study_store.MAX_BOX, "known"))
        self.assertEqual(study_store.next_box(box, True, TODAY)[0], study_store.MAX_BOX)  # 上限で止まる
        self.assertEqual(study_store.next_box(4, False, TODAY), (0, "2026-11-20", "learning"))

    def test_review_due_and_known(self):
        with self.conn() as conn:
            study_store.save_transcript(conn, "new", SEGMENTS, "small.en")
            study_store.save_study(conn, "new", "", VOCAB, today=TODAY)
            ids = [v["id"] for v in study_store.list_vocab(conn, guid="new")]
            self.assertEqual(len(study_store.list_vocab(conn, due_only=True, today=TODAY)), 2)
            study_store.review_vocab(conn, ids[0], True, today=TODAY)
            study_store.set_vocab_known(conn, ids[1], True, today=TODAY)
            self.assertEqual(study_store.list_vocab(conn, due_only=True, today=TODAY), [])
            stats = study_store.study_stats(conn, today=TODAY)
            self.assertEqual(stats, {"ready": 1, "vocab": 2, "known": 1, "due": 0})
            back = study_store.set_vocab_known(conn, ids[1], False, today=TODAY)
            self.assertEqual((back["status"], back["next_review"]), ("learning", "2026-11-20"))
            self.assertIsNone(study_store.review_vocab(conn, 9999, True))

    def test_study_batch_does_not_change_morning_status(self):
        with self.conn() as conn:
            start_batch_log(conn, "study", date.today().isoformat())
            self.assertIsNone(store.today_batch(conn))
            self.assertEqual(store.recent_batches(conn), [])


if __name__ == "__main__":
    unittest.main()
