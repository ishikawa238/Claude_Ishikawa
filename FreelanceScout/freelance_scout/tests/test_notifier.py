"""
notifier モジュールのテスト

send_webhook をモックして Block Kit ペイロードの構造を検証する。
"""

import unittest
from datetime import date
from unittest.mock import patch

from src.notifier import send_job_report, _job_block, _append_job_blocks


def _make_job(
    source="crowdworks",
    job_id="100",
    title="Python スクレイピング案件",
    budget="50,000〜100,000円",
    matched_keywords="Python, スクレイピング",
    **kwargs,
) -> dict:
    job = {
        "source": source,
        "job_id": job_id,
        "title": title,
        "url": f"https://example.com/jobs/{job_id}",
        "description": "テスト案件の説明文",
        "budget": budget,
        "category": "開発",
        "posted_date": "2026-04-25",
        "matched_keywords": matched_keywords,
    }
    job.update(kwargs)
    return job


class TestJobBlock(unittest.TestCase):
    """_job_block のテスト。"""

    def test_all_fields(self):
        job = _make_job()
        block = _job_block(job)
        text = block["text"]["text"]
        self.assertIn("Python スクレイピング案件", text)
        self.assertIn("50,000〜100,000円", text)
        self.assertIn("マッチ: Python, スクレイピング", text)
        self.assertIn("詳細を見る", text)

    def test_no_budget(self):
        job = _make_job(budget="")
        block = _job_block(job)
        text = block["text"]["text"]
        self.assertNotIn("円", text)

    def test_no_keywords(self):
        job = _make_job(matched_keywords="")
        block = _job_block(job)
        text = block["text"]["text"]
        self.assertNotIn("マッチ", text)


class TestAppendJobBlocks(unittest.TestCase):
    """_append_job_blocks の省略表示テスト。"""

    @patch("src.notifier._MAX_PER_SITE", 3)
    def test_within_limit(self):
        blocks = []
        jobs = [_make_job(job_id=str(i)) for i in range(3)]
        _append_job_blocks(blocks, jobs)
        # 3件分のブロックのみ（省略なし）
        self.assertEqual(len(blocks), 3)

    @patch("src.notifier._MAX_PER_SITE", 2)
    def test_exceeds_limit(self):
        blocks = []
        jobs = [_make_job(job_id=str(i)) for i in range(5)]
        _append_job_blocks(blocks, jobs)
        # 2件分 + 省略表示1件 = 3ブロック
        self.assertEqual(len(blocks), 3)
        last_text = blocks[-1]["text"]["text"]
        self.assertIn("他 3 件", last_text)


class TestSendJobReport(unittest.TestCase):
    """send_job_report の統合テスト。"""

    @patch("src.notifier.send_webhook", return_value=True)
    def test_sends_report(self, mock_send):
        jobs = [
            _make_job(source="crowdworks", job_id="1"),
            _make_job(source="coconala", job_id="2"),
        ]
        stats = {"crowdworks_found": 100, "coconala_found": 50}
        result = send_job_report(date(2026, 4, 25), jobs, stats)

        self.assertTrue(result)
        mock_send.assert_called_once()

        payload = mock_send.call_args[0][1]
        blocks = payload["blocks"]
        texts = " ".join(
            b.get("text", {}).get("text", "") for b in blocks if b["type"] == "section"
        )
        self.assertIn("2026-04-25", texts)
        self.assertIn("新着: *2件*", texts)
        self.assertIn("CrowdWorks", texts)
        self.assertIn("Coconala", texts)

    def test_empty_jobs_skips(self):
        stats = {"crowdworks_found": 0, "coconala_found": 0}
        result = send_job_report(date(2026, 4, 25), [], stats)
        self.assertTrue(result)

    @patch("src.notifier.send_webhook", return_value=True)
    def test_only_crowdworks(self, mock_send):
        jobs = [_make_job(source="crowdworks", job_id="1")]
        stats = {"crowdworks_found": 50, "coconala_found": 0}
        result = send_job_report(date(2026, 4, 25), jobs, stats)

        self.assertTrue(result)
        payload = mock_send.call_args[0][1]
        texts = " ".join(
            b.get("text", {}).get("text", "") for b in payload["blocks"] if b["type"] == "section"
        )
        self.assertIn("CrowdWorks", texts)
        self.assertNotIn("Coconala", texts)

    @patch("src.notifier.send_webhook", return_value=False)
    def test_webhook_failure(self, mock_send):
        jobs = [_make_job()]
        stats = {"crowdworks_found": 10, "coconala_found": 0}
        result = send_job_report(date(2026, 4, 25), jobs, stats)
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
