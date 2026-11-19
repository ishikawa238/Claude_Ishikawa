import os
import unittest
from unittest import mock

from shared.db import fetch_all
from src.study import ai, runner
from src.study import store as study_store
from tests.helpers import TempEnvTestCase

SEGMENTS = [{"start": 0.0, "end": 1.0, "text": "Hello."}, {"start": 1.0, "end": 2.0, "text": "Bye."}]
VOCAB = [{"term": "bye", "kind": "word", "meaning_ja": "さよなら", "example_en": "Bye.", "example_ja": "またね。",
          "segment_idx": 1}]


class RunnerTest(TempEnvTestCase):
    def setUp(self):
        super().setUp()
        self.env = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def set_study(self, **cfg):
        self.config["study"] = cfg
        from shared.config import get_config
        get_config()["study"] = cfg

    def batches(self):
        with self.conn() as conn:
            return [dict(r) for r in fetch_all(conn, "SELECT * FROM batch_run_log WHERE batch_type = 'study'")]

    def patch(self, target, attr, **kw):
        p = mock.patch.object(target, attr, **kw)
        m = p.start()
        self.addCleanup(p.stop)
        return m

    def test_pick_within_budget(self):
        eps = [{"duration_sec": 50 * 60}, {"duration_sec": 50 * 60}, {"duration_sec": 30 * 60}, {"duration_sec": 10 * 60}]
        self.assertEqual(len(runner.pick_within_budget(eps, 90)), 3)  # 50 + 30 + 10（2本目の50は入らない）
        self.assertEqual(len(runner.pick_within_budget([{"duration_sec": 200 * 60}], 90)), 1)

    def test_transcribes_and_skips_ai_without_key(self):
        self.add_episode_file("a")
        tr = self.patch(runner.transcribe, "transcribe", return_value=SEGMENTS)
        result = runner.run()
        self.assertEqual((result.transcribed, result.studied, result.ai_skipped), (1, 0, True))
        tr.assert_called_once()
        with self.conn() as conn:
            t = study_store.get_transcript(conn, "a")
        self.assertEqual((t["status"], t["ai_status"], len(t["segments"])), ("done", "skipped", 2))
        self.assertEqual(self.batches()[0]["status"], "success")

    def test_full_flow_with_ai(self):
        self.set_study(anthropic_api_key="sk-test")
        self.add_episode_file("a")
        self.patch(runner.transcribe, "transcribe", return_value=SEGMENTS)
        self.patch(runner.ai, "make_client", return_value=object())
        self.patch(runner.ai, "translate", return_value={0: "こんにちは。", 1: "さようなら。"})
        self.patch(runner.ai, "extract_study", return_value=("要約", VOCAB))
        result = runner.run()
        self.assertEqual((result.transcribed, result.studied, result.failed), (1, 1, 0))
        with self.conn() as conn:
            t = study_store.get_transcript(conn, "a")
        self.assertEqual(t["ai_status"], "done")
        self.assertEqual(t["segments"][1]["text_ja"], "さようなら。")
        self.assertEqual([v["term"] for v in t["vocab"]], ["bye"])

    def test_failures_are_recorded_and_others_continue(self):
        self.set_study(anthropic_api_key="sk-test")
        self.add_episode_file("bad", published="2026-11-19 07:00:00")
        self.add_episode_file("good", published="2026-11-18 07:00:00")
        self.patch(runner.transcribe, "transcribe", side_effect=[RuntimeError("decode error"), SEGMENTS])
        self.patch(runner.ai, "make_client", return_value=object())
        self.patch(runner.ai, "translate", side_effect=ai.StudyAIError("HTTP 529"))
        result = runner.run()
        self.assertEqual((result.transcribed, result.studied, result.failed), (1, 0, 2))
        with self.conn() as conn:
            bad = study_store.get_transcript(conn, "bad")
            good = study_store.get_transcript(conn, "good")
        self.assertEqual((bad["status"], bad["error"]), ("failed", "decode error"))
        self.assertEqual(good["ai_status"], "failed")
        self.assertEqual(self.batches()[0]["status"], "partial_failure")

    def test_ai_retried_next_run_without_retranscribing(self):
        self.set_study(anthropic_api_key="sk-test")
        self.add_episode_file("a")
        with self.conn() as conn:
            study_store.save_transcript(conn, "a", SEGMENTS, "small.en")
            study_store.set_ai_status(conn, "a", "failed", "x")
        tr = self.patch(runner.transcribe, "transcribe")
        self.patch(runner.ai, "make_client", return_value=object())
        self.patch(runner.ai, "translate", return_value={0: "a", 1: "b"})
        self.patch(runner.ai, "extract_study", return_value=("", []))
        self.assertEqual(runner.run().studied, 1)
        tr.assert_not_called()

    def test_missing_file_marks_failed(self):
        self.add_episode_file("a")
        (self.root / "library" / "a.mp3").unlink()
        tr = self.patch(runner.transcribe, "transcribe")
        self.assertEqual(runner.run().failed, 1)
        tr.assert_not_called()

    def test_whisper_not_installed_fails_whole_batch(self):
        self.add_episode_file("a")
        self.patch(runner.transcribe, "transcribe", side_effect=ImportError("no faster_whisper"))
        with self.assertRaises(ImportError):
            runner.run()
        self.assertEqual(self.batches()[0]["status"], "failed")
        with self.conn() as conn:
            self.assertEqual(len(study_store.transcript_targets(conn)), 1)  # 失敗回数は使わない

    def test_dry_run_writes_nothing(self):
        self.add_episode_file("a")
        tr = self.patch(runner.transcribe, "transcribe")
        result = runner.run(runner.StudyOptions(dry_run=True))
        self.assertEqual(result.planned, ["a"])
        tr.assert_not_called()
        self.assertEqual(self.batches(), [])

    def test_single_guid_and_disabled(self):
        self.set_study(enabled=False)
        self.add_episode_file("a")
        self.add_episode_file("b")
        tr = self.patch(runner.transcribe, "transcribe", return_value=SEGMENTS)
        self.assertEqual(runner.run().transcribed, 0)  # お休み中は何もしない
        self.assertEqual(runner.run(runner.StudyOptions(guid="b")).transcribed, 1)  # 手動の1話は動く
        self.assertEqual(tr.call_count, 1)
        with self.assertRaises(ValueError):
            runner.run(runner.StudyOptions(guid="nope"))

    def test_refuses_when_already_running(self):
        from shared.batch import start_batch_log
        with self.conn() as conn:
            start_batch_log(conn, "study", "2026-11-19")
        with self.assertRaises(runner.AlreadyRunningError):
            runner.run()


if __name__ == "__main__":
    unittest.main()
