import unittest
from unittest import mock

from src import store
from src.web import app as web_app
from tests.helpers import TempEnvTestCase

NO_TASK = {"exists": False}


class WebTest(TempEnvTestCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(web_app.scheduler_info, "get_task_status", return_value=NO_TASK)
        p.start()
        self.addCleanup(p.stop)
        web_app.status.clear_task_cache()
        self.app = web_app.create_app(self.config_path)
        self.client = self.app.test_client()

    def post(self, url, body=None, method="post"):
        return getattr(self.client, method)(url, json=body or {}, base_url="http://127.0.0.1:5210")

    def get(self, url):
        return self.client.get(url, base_url="http://127.0.0.1:5210")

    def test_pages_render(self):
        ep = self.add_episode_file("a", duration=600)
        with self.conn() as conn:
            hid = store.save_history(conn, [(ep, "new")], schedule_label="M", target_minutes=30, trigger="schedule")
        for url in ["/", "/today", "/history", f"/history/{hid}", "/library", "/library?show=Show+A&q=Title",
                    "/feeds", "/schedule", "/health"]:
            with self.subTest(url=url):
                self.assertEqual(self.get(url).status_code, 200)

    def test_pages_render_when_empty(self):
        for url in ["/", "/today", "/history", "/library"]:
            with self.subTest(url=url):
                self.assertEqual(self.get(url).status_code, 200)

    def test_art_route_serves_only_hashed_names(self):
        from src import artwork

        artwork.art_dir().mkdir(parents=True, exist_ok=True)
        name = "a" * 40 + ".jpg"
        (artwork.art_dir() / name).write_bytes(b"\xff\xd8jpeg")
        res = self.get(f"/art/{name}")
        self.assertEqual(res.status_code, 200)
        res.close()  # 送信ファイルを閉じる（Windows で一時フォルダを消せるように）
        for bad in ["/art/..%2Fconfig.json", "/art/abc.jpg", "/art/" + "A" * 40 + ".jpg", "/art/" + "a" * 40 + ".png"]:
            with self.subTest(bad=bad):
                self.assertEqual(self.get(bad).status_code, 404)

    def test_pages_use_artwork_and_placeholder(self):
        art = "b" * 40 + ".jpg"
        ep = self.add_episode_file("a", show="Show A", duration=600)
        self.add_episode_file("b", show="No Art", duration=600)
        with self.conn() as conn:
            store.upsert_show(conn, "Show A", "https://x/a.jpg", art, "#123456")
            store.save_history(conn, [(ep, "new")], schedule_label="M", target_minutes=30, trigger="schedule")
        html = self.get("/library").get_data(as_text=True)
        self.assertIn(f"/art/{art}", html)
        self.assertIn("art ph", html)  # 画像のない番組はプレースホルダ
        home = self.get("/").get_data(as_text=True)
        self.assertIn("--hero: #123456", home)

    def test_mosaic_items_unique_or_single(self):
        a = {"show": "A", "art_file": "1" * 40 + ".jpg"}
        b = {"show": "B", "art_file": "2" * 40 + ".jpg"}
        c = {"show": "C", "art_file": "3" * 40 + ".jpg"}
        d = {"show": "D", "art_file": "4" * 40 + ".jpg"}
        with self.app.test_request_context(base_url="http://127.0.0.1:5210"):
            self.assertEqual(len(web_app.mosaic_items([a, a, b, c, d])), 4)
            self.assertEqual(web_app.mosaic_items([a, a, b]), [a])
            self.assertEqual(web_app.mosaic_items([]), [])

    def test_foreign_host_rejected(self):
        res = self.client.get("/", base_url="http://evil.example:5210")
        self.assertEqual(res.status_code, 403)

    def test_form_post_rejected(self):
        res = self.client.post("/api/run", data="x", base_url="http://127.0.0.1:5210")
        self.assertEqual(res.status_code, 415)

    def test_put_feeds_validates_and_saves(self):
        bad = self.post("/api/feeds", {"feeds": [{"name": "X", "url": "ftp://x"}]}, "put")
        self.assertEqual(bad.status_code, 400)
        good = self.post("/api/feeds", {"feeds": [{"name": "X", "url": "https://x/rss"}], "episodes_per_feed": 2}, "put")
        self.assertTrue(good.get_json()["ok"])
        raw = web_app.config_store.load_raw(self.config_path)
        self.assertEqual(raw["radio"]["feeds"][0]["name"], "X")
        self.assertEqual(raw["radio"]["episodes_per_feed"], 2)

    def test_put_schedules_saves_and_reregisters(self):
        with mock.patch.object(web_app.scheduler_info, "reregister", return_value=(True, "ok")) as rereg:
            res = self.post("/api/schedules", {"schedules": [{"start": "06:30", "end": "07:15"}]}, "put")
        self.assertTrue(res.get_json()["ok"])
        rereg.assert_called_once()
        raw = web_app.config_store.load_raw(self.config_path)
        self.assertEqual(raw["radio"]["schedules"][0]["start"], "06:30")
        self.assertEqual(raw["radio"]["target_minutes"], 45)

    def test_put_schedules_invalid_is_not_saved(self):
        with mock.patch.object(web_app.scheduler_info, "reregister") as rereg:
            res = self.post("/api/schedules", {"schedules": [{"start": "08:00", "end": "07:00"}]}, "put")
        self.assertEqual(res.status_code, 400)
        rereg.assert_not_called()

    def test_play_episode_outside_library_refused(self):
        outside = self.root / "outside.mp3"
        outside.write_bytes(b"x")
        with self.conn() as conn:
            store.add_episode(conn, {"guid": "evil", "show": "S", "title": "T", "path": str(outside), "duration_sec": 60})
        with mock.patch.object(web_app.player, "play") as play:
            res = self.post("/api/play/episode", {"guid": "evil"})
        self.assertEqual(res.status_code, 400)
        play.assert_not_called()

    def test_play_history_records_new_history(self):
        ep = self.add_episode_file("a", duration=600)
        with self.conn() as conn:
            hid = store.save_history(conn, [(ep, "new")], schedule_label="M", target_minutes=30, trigger="schedule")
        with mock.patch.object(web_app.player, "play", return_value="vlc") as play:
            res = self.post(f"/api/play/history/{hid}")
        self.assertTrue(res.get_json()["ok"])
        play.assert_called_once()
        with self.conn() as conn:
            self.assertEqual(store.count_history(conn), 2)
            self.assertEqual(store.get_episode(conn, "a")["play_count"], 1)

    def test_play_without_vlc_returns_message(self):
        self.add_episode_file("a", duration=600)
        with mock.patch.object(web_app.player, "find_vlc", return_value=None):
            res = self.post("/api/play/episode", {"guid": "a"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("VLC", res.get_json()["message"])

    def test_run_rejected_while_running(self):
        from shared.batch import start_batch_log

        with self.conn() as conn:
            start_batch_log(conn, "morning", "2026-11-19")
        with mock.patch.object(web_app.threading, "Thread") as thread:
            res = self.post("/api/run")
        self.assertEqual(res.status_code, 409)
        thread.assert_not_called()

    def test_run_starts_background_thread(self):
        with mock.patch.object(web_app.threading, "Thread") as thread:
            res = self.post("/api/run")
        self.assertTrue(res.get_json()["ok"])
        thread.return_value.start.assert_called_once()
        web_app._run_lock.release()  # スレッドを実際には動かしていないので手動で解放


if __name__ == "__main__":
    unittest.main()
