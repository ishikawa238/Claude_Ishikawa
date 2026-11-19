import unittest
from unittest import mock

from src import radio, store
from tests.helpers import TempEnvTestCase


class RunTest(TempEnvTestCase):
    def _patch(self, new_entries):
        return [
            mock.patch.object(radio.feeds, "fetch_all", return_value=([], {})),
            mock.patch.object(radio.library, "download_new", return_value=new_entries),
            mock.patch.object(radio.player, "play", return_value="vlc.exe"),
            mock.patch.object(radio.notifier, "send_success"),
            mock.patch.object(radio.notifier, "send_failure"),
        ]

    def _run(self, new_entries, **opts):
        patches = self._patch(new_entries)
        mocks = [p.start() for p in patches]
        self.addCleanup(lambda: [p.stop() for p in patches])
        return radio.run(radio.RunOptions(**opts)), mocks

    def test_plays_library_when_no_new_and_records(self):
        self.add_episode_file("old", duration=600)
        result, mocks = self._run([], trigger="manual")
        self.assertEqual([(e["guid"], s) for e, s in result.queue], [("old", "shuffle")])
        mocks[2].assert_called_once()
        mocks[3].assert_called_once()
        with self.conn() as conn:
            batch = store.today_batch(conn)
            ep = store.get_episode(conn, "old")
            latest = store.get_latest_history(conn)
        self.assertEqual(batch["status"], "success")
        self.assertEqual(batch["queued_episodes"], 1)
        self.assertEqual(ep["play_count"], 1)
        self.assertEqual(latest["status"], "played")
        self.assertTrue((self.root / "data" / "today.m3u").is_file())

    def test_empty_library_fails_and_notifies(self):
        patches = self._patch([])
        mocks = [p.start() for p in patches]
        self.addCleanup(lambda: [p.stop() for p in patches])
        with self.assertRaises(RuntimeError):
            radio.run(radio.RunOptions())
        mocks[4].assert_called_once()
        with self.conn() as conn:
            self.assertEqual(store.today_batch(conn)["status"], "failed")

    def test_no_play_does_not_mark_played_or_notify(self):
        self.add_episode_file("old", duration=600)
        _, mocks = self._run([], play=False)
        mocks[2].assert_not_called()
        mocks[3].assert_not_called()
        with self.conn() as conn:
            self.assertEqual(store.get_episode(conn, "old")["play_count"], 0)
            self.assertEqual(store.get_latest_history(conn)["status"], "queued")

    def test_dry_run_leaves_no_batch_log(self):
        self.add_episode_file("old", duration=600)
        self._run([], dry_run=True)
        with self.conn() as conn:
            self.assertIsNone(store.today_batch(conn))
            self.assertIsNone(store.get_latest_history(conn))

    def test_already_running_is_rejected(self):
        from shared.batch import start_batch_log

        with self.conn() as conn:
            start_batch_log(conn, "morning", "2026-11-19")
        with self.assertRaises(radio.AlreadyRunningError):
            self._run([])

    def test_resolve_target_uses_schedule(self):
        from datetime import datetime

        label, minutes = radio.resolve_target(self.config["radio"], datetime(2026, 11, 19, 7, 2))
        self.assertEqual((label, minutes), ("Morning", 30))
        label, minutes = radio.resolve_target(self.config["radio"], datetime(2026, 11, 19, 15, 0))
        self.assertEqual(minutes, 30)  # target_minutes


if __name__ == "__main__":
    unittest.main()
