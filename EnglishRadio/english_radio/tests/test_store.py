import unittest

from src import config_store, store
from tests.helpers import TempEnvTestCase


class StoreTest(TempEnvTestCase):
    def test_history_roundtrip_and_stats(self):
        a = self.add_episode_file("a", show="A", duration=600)
        b = self.add_episode_file("b", show="B", duration=300)
        with self.conn() as conn:
            hid = store.save_history(conn, [(a, "new"), (b, "shuffle")],
                                     schedule_label="Morning", target_minutes=30, trigger="schedule")
            store.mark_played(conn, ["a"])
        with self.conn() as conn:
            h = store.get_history(conn, hid)
            stats = store.library_stats(conn)
            latest = store.get_latest_history(conn)
            rows = store.list_history(conn)
        self.assertEqual([i["guid"] for i in h["items"]], ["a", "b"])
        self.assertEqual([i["source"] for i in h["items"]], ["new", "shuffle"])
        self.assertEqual(h["total_sec"], 900)
        self.assertEqual(latest["id"], hid)
        self.assertEqual(rows[0]["item_count"], 2)
        self.assertEqual(stats["count"], 2)
        self.assertEqual(stats["unplayed"], 1)
        self.assertEqual({s["show"] for s in stats["by_show"]}, {"A", "B"})

    def test_list_episodes_filters(self):
        self.add_episode_file("a", show="A")
        self.add_episode_file("b", show="B")
        with self.conn() as conn:
            self.assertEqual([e["guid"] for e in store.list_episodes(conn, show="B")], ["b"])
            self.assertEqual([e["guid"] for e in store.list_episodes(conn, query="Title a")], ["a"])

    def test_get_history_missing(self):
        with self.conn() as conn:
            self.assertIsNone(store.get_history(conn, 999))
            self.assertIsNone(store.get_latest_history(conn))


class ConfigStoreTest(TempEnvTestCase):
    def test_validate_feeds(self):
        cleaned = config_store.validate_feeds([{"name": " A ", "url": "https://a/rss"}])
        self.assertEqual(cleaned, [{"name": "A", "url": "https://a/rss", "enabled": True}])
        bad_cases = [
            [{"name": "", "url": "https://a"}],
            [{"name": "A", "url": "ftp://a"}],
            [{"name": "A", "url": "https://a"}, {"name": "B", "url": "https://a"}],
        ]
        for bad in bad_cases:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config_store.validate_feeds(bad)

    def test_update_radio_keeps_other_keys(self):
        cfg = config_store.update_radio(self.config_path, {"episodes_per_feed": 5})
        self.assertEqual(cfg["radio"]["episodes_per_feed"], 5)
        raw = config_store.load_raw(self.config_path)
        self.assertEqual(raw["radio"]["episodes_per_feed"], 5)
        self.assertEqual(raw["radio"]["feeds"][0]["name"], "Show A")
        self.assertEqual(list(self.root.glob(".config_*")), [])

    def test_episodes_per_feed_range(self):
        self.assertEqual(config_store.validate_episodes_per_feed("3"), 3)
        for bad in (0, 11, "x", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config_store.validate_episodes_per_feed(bad)


if __name__ == "__main__":
    unittest.main()
