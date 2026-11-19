import unittest
from unittest import mock

from src import store
from src.study import store as study_store
from src.web import app as web_app
from src.web import study_routes
from tests.helpers import TempEnvTestCase

SEGMENTS = [{"start": 0.0, "end": 1.0, "text": "Hello there."}]
VOCAB = [{"term": "hello", "kind": "word", "meaning_ja": "こんにちは", "segment_idx": 0}]
BASE = "http://127.0.0.1:5210"


class StudyWebTest(TempEnvTestCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(web_app.scheduler_info, "get_task_status", return_value={"exists": False})
        p.start()
        self.addCleanup(p.stop)
        web_app.status.clear_task_cache()
        self.client = web_app.create_app(self.config_path).test_client()

    def get(self, url, **kw):
        return self.client.get(url, base_url=BASE, **kw)

    def with_transcript(self, guid="a"):
        self.add_episode_file(guid)
        with self.conn() as conn:
            study_store.save_transcript(conn, guid, SEGMENTS, "small.en")
            study_store.save_study(conn, guid, "要約", VOCAB)

    def test_pages_render(self):
        self.with_transcript()
        self.add_episode_file("b")
        for url in ["/study", "/study/episode?guid=a", "/study/episode?guid=b", "/vocab", "/vocab?mode=review",
                    "/vocab?status=known&show=Show+A", "/releases", "/", "/library"]:
            with self.subTest(url=url):
                self.assertEqual(self.get(url).status_code, 200)
        self.assertEqual(self.get("/study/episode?guid=none").status_code, 302)
        self.assertIn("Hello there.", self.get("/study/episode?guid=a").get_data(as_text=True))
        self.assertIn("/study/episode?guid=a", self.get("/library").get_data(as_text=True))

    def test_audio_serves_library_file_with_range_and_refuses_outside(self):
        self.add_episode_file("a")
        res = self.get("/audio?guid=a", headers={"Range": "bytes=0-2"})
        self.assertEqual(res.status_code, 206)
        self.assertEqual(res.data, b"ID3")
        res.close()
        outside = self.root / "outside.mp3"
        outside.write_bytes(b"x")
        with self.conn() as conn:
            store.add_episode(conn, {"guid": "evil", "show": "S", "title": "T", "path": str(outside)})
        self.assertEqual(self.get("/audio?guid=evil").status_code, 404)
        self.assertEqual(self.get("/audio?guid=nope").status_code, 404)

    def test_vocab_api(self):
        self.with_transcript()
        with self.conn() as conn:
            vid = study_store.list_vocab(conn)[0]["id"]
        put = lambda body: self.client.put(f"/api/vocab/{vid}", json=body, base_url=BASE)
        self.assertEqual(put({"result": "ok"}).get_json()["word"]["box"], 1)
        self.assertEqual(put({"known": True}).get_json()["word"]["status"], "known")
        self.assertEqual(put({}).status_code, 400)
        self.assertEqual(self.client.put("/api/vocab/999", json={"known": True}, base_url=BASE).status_code, 404)
        self.assertEqual(self.client.put(f"/api/vocab/{vid}", data="x", base_url=BASE).status_code, 415)
        self.assertEqual(self.client.put(f"/api/vocab/{vid}", json={"known": True},
                                         base_url="http://evil.example").status_code, 403)

    def test_transcribe_api_starts_thread_and_rejects_while_running(self):
        self.add_episode_file("a")
        with mock.patch.object(study_routes.threading, "Thread") as thread:
            res = self.client.post("/api/study/transcribe", json={"guid": "a"}, base_url=BASE)
            self.assertTrue(res.get_json()["ok"])
            busy = self.client.post("/api/study/run", json={}, base_url=BASE)
            self.assertEqual(busy.status_code, 409)
        thread.return_value.start.assert_called_once()
        study_routes._study_lock.release()
        missing = self.client.post("/api/study/transcribe", json={"guid": "x"}, base_url=BASE)
        self.assertEqual(missing.status_code, 404)

    def test_group_study_episodes(self):
        groups = study_routes.group_study_episodes(
            [{"t_status": "done"}, {"t_status": None}, {"t_status": "failed"}, {"t_status": None}])
        self.assertEqual({k: len(v) for k, v in groups.items()}, {"ready": 1, "pending": 2, "failed": 1})


if __name__ == "__main__":
    unittest.main()
