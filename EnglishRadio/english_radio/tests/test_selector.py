import random
import unittest

from src.selector import NEW, SHUFFLE, build_queue
from tests.helpers import make_entry


class BuildQueueTest(unittest.TestCase):
    def test_round_robin_across_shows(self):
        new = [
            make_entry("a1", "A", 600, "2026-11-19 07:00:00"),
            make_entry("a2", "A", 600, "2026-11-18 07:00:00"),
            make_entry("b1", "B", 600, "2026-11-19 06:00:00"),
        ]
        queue = build_queue(new, [], target_minutes=20, rng=random.Random(0))
        self.assertEqual([e["guid"] for e, _ in queue], ["a1", "b1"])
        self.assertTrue(all(src == NEW for _, src in queue))

    def test_never_exceeds_target(self):
        new = [make_entry(f"n{i}", f"S{i}", 700) for i in range(10)]
        library = [make_entry(f"l{i}", "L", 500) for i in range(10)]
        queue = build_queue(new, library, target_minutes=30, rng=random.Random(1))
        self.assertLessEqual(sum(e["duration_sec"] for e, _ in queue), 30 * 60)

    def test_fills_remaining_time_from_library(self):
        new = [make_entry("n1", "A", 600)]
        library = [new[0], make_entry("l1", "B", 600), make_entry("l2", "C", 600)]
        queue = build_queue(new, library, target_minutes=30, rng=random.Random(2))
        guids = [e["guid"] for e, _ in queue]
        self.assertEqual(guids[0], "n1")
        self.assertEqual(sorted(guids[1:]), ["l1", "l2"])
        self.assertEqual([s for _, s in queue], [NEW, SHUFFLE, SHUFFLE])

    def test_no_new_episodes_still_plays_library(self):
        library = [make_entry("l1", "A", 600)]
        queue = build_queue([], library, target_minutes=30)
        self.assertEqual([(e["guid"], s) for e, s in queue], [("l1", SHUFFLE)])

    def test_unplayed_preferred_over_recently_played(self):
        library = [
            make_entry("played", "A", 900, last_played="2026-11-18 07:00:00"),
            make_entry("fresh", "B", 900),
        ]
        queue = build_queue([], library, target_minutes=15, rng=random.Random(3))
        self.assertEqual([e["guid"] for e, _ in queue], ["fresh"])

    def test_oldest_played_preferred(self):
        library = [
            make_entry("recent", "A", 900, last_played="2026-11-18 07:00:00"),
            make_entry("old", "B", 900, last_played="2026-10-01 07:00:00"),
        ]
        queue = build_queue([], library, target_minutes=15, rng=random.Random(4))
        self.assertEqual([e["guid"] for e, _ in queue], ["old"])

    def test_reproducible_with_seed(self):
        library = [make_entry(f"l{i}", "L", 300) for i in range(20)]
        q1 = build_queue([], library, 30, random.Random(42))
        q2 = build_queue([], library, 30, random.Random(42))
        self.assertEqual([e["guid"] for e, _ in q1], [e["guid"] for e, _ in q2])

    def test_unknown_duration_is_skipped(self):
        library = [make_entry("x", "A", None)]
        self.assertEqual(build_queue([], library, 30), [])


if __name__ == "__main__":
    unittest.main()
