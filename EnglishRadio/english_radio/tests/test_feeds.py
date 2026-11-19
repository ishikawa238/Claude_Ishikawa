import unittest
from unittest import mock

from src import feeds
from tests.helpers import TempEnvTestCase

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
<channel>
  <title>Sample Show</title>
  <itunes:image href="https://example.com/show.jpg"/>
  <item>
    <title>Old episode</title>
    <description><![CDATA[<p>Hello &amp; <b>welcome</b></p>
<p>to   the show.</p>]]></description>
    <guid>ep-1</guid>
    <pubDate>Mon, 17 Nov 2026 07:00:00 GMT</pubDate>
    <itunes:duration>12:30</itunes:duration>
    <enclosure url="https://example.com/1.mp3" type="audio/mpeg" length="1"/>
  </item>
  <item>
    <title>New episode</title>
    <guid>ep-2</guid>
    <pubDate>Wed, 19 Nov 2026 07:00:00 GMT</pubDate>
    <itunes:duration>1:02:03</itunes:duration>
    <itunes:image href="https://example.com/ep2.jpg"/>
    <enclosure url="https://example.com/2.mp3" type="audio/mpeg" length="1"/>
  </item>
  <item>
    <title>Video only</title>
    <guid>ep-3</guid>
    <pubDate>Thu, 20 Nov 2026 07:00:00 GMT</pubDate>
    <enclosure url="https://example.com/3.mp4" type="video/mp4" length="1"/>
  </item>
</channel>
</rss>"""


class ParseDurationTest(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(feeds.parse_duration("1:02:03"), 3723)
        self.assertEqual(feeds.parse_duration("12:30"), 750)
        self.assertEqual(feeds.parse_duration("900"), 900)
        self.assertEqual(feeds.parse_duration(900), 900)

    def test_invalid(self):
        self.assertIsNone(feeds.parse_duration(None))
        self.assertIsNone(feeds.parse_duration(""))
        self.assertIsNone(feeds.parse_duration("abc"))
        self.assertIsNone(feeds.parse_duration("0"))


class ParseFeedTest(unittest.TestCase):
    def test_newest_first_and_audio_only(self):
        title, eps = feeds.parse_feed(SAMPLE_RSS, "My Show", limit=5)
        self.assertEqual(title, "Sample Show")
        self.assertEqual([e.guid for e in eps], ["ep-2", "ep-1"])
        self.assertEqual(eps[0].show, "My Show")
        self.assertEqual(eps[0].duration_sec, 3723)
        self.assertEqual(eps[0].published, "2026-11-19 07:00:00")

    def test_episode_image_falls_back_to_show_image(self):
        _, eps = feeds.parse_feed(SAMPLE_RSS, "My Show", limit=5)
        self.assertEqual(eps[0].image_url, "https://example.com/ep2.jpg")
        self.assertEqual(eps[1].image_url, "https://example.com/show.jpg")

    def test_summary_is_plain_text(self):
        _, eps = feeds.parse_feed(SAMPLE_RSS, "My Show", limit=5)
        self.assertEqual(eps[1].summary, "Hello & welcome to the show.")

    def test_clean_summary_truncates(self):
        text = feeds.clean_summary("a" * 500)
        self.assertEqual(len(text), feeds.SUMMARY_MAX_LEN)
        self.assertTrue(text.endswith("…"))

    def test_feed_result_has_show_image(self):
        result = feeds.parse_feed_result(SAMPLE_RSS, "My Show", limit=5)
        self.assertEqual(result.image_url, "https://example.com/show.jpg")

    def test_limit(self):
        _, eps = feeds.parse_feed(SAMPLE_RSS, "My Show", limit=1)
        self.assertEqual(len(eps), 1)

    def test_show_name_falls_back_to_feed_title(self):
        _, eps = feeds.parse_feed(SAMPLE_RSS, "", limit=1)
        self.assertEqual(eps[0].show, "Sample Show")


class DownloadFeedTest(unittest.TestCase):
    def test_rejects_non_http(self):
        with self.assertRaises(ValueError):
            feeds.download_feed("file:///etc/passwd")


class FetchAllTest(TempEnvTestCase):
    def test_failed_feed_is_skipped(self):
        feed_list = [
            {"name": "Broken", "url": "https://bad.example/rss"},
            {"name": "Good", "url": "https://good.example/rss"},
            {"name": "Off", "url": "https://off.example/rss", "enabled": False},
        ]

        def fake_download(url, session=None):
            if "bad" in url:
                raise ConnectionError("down")
            if "off" in url:
                raise AssertionError("無効な番組は取得しない")
            return SAMPLE_RSS.encode()

        with mock.patch.object(feeds, "download_feed", side_effect=fake_download):
            eps, images = feeds.fetch_all(feed_list, limit=3)
        self.assertEqual({e.show for e in eps}, {"Good"})
        self.assertEqual(len(eps), 2)
        self.assertEqual(images, {"Good": "https://example.com/show.jpg"})


if __name__ == "__main__":
    unittest.main()
