"""
job_filter モジュールのテスト

DB 接続はインメモリ SQLite を使用する。
"""

import sqlite3
import unittest

from src.job_filter import (
    _exclude_by_keywords,
    _exclude_existing,
    _match_keywords,
    filter_new_jobs,
)


def _make_conn() -> sqlite3.Connection:
    """テスト用インメモリ DB を作成する。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            job_id TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            description TEXT,
            budget TEXT,
            category TEXT,
            posted_date TEXT,
            fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            notified_at DATETIME,
            matched_keywords TEXT
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX idx_jobs_source_job_id ON jobs(source, job_id)"
    )
    return conn


def _make_job(
    source="crowdworks",
    job_id="100",
    title="Python スクレイピング案件",
    description="Webデータを自動収集するシステムの開発",
    **kwargs,
) -> dict:
    """テスト用案件データを生成する。"""
    job = {
        "source": source,
        "job_id": job_id,
        "title": title,
        "url": f"https://example.com/jobs/{job_id}",
        "description": description,
        "budget": "50,000〜100,000円",
        "category": "開発",
        "posted_date": "2026-04-25",
    }
    job.update(kwargs)
    return job


class TestExcludeExisting(unittest.TestCase):
    """重複排除のテスト。"""

    def test_new_jobs_pass_through(self):
        conn = _make_conn()
        jobs = [_make_job(job_id="1"), _make_job(job_id="2")]
        result = _exclude_existing(jobs, conn)
        self.assertEqual(len(result), 2)

    def test_existing_jobs_excluded(self):
        conn = _make_conn()
        conn.execute(
            "INSERT INTO jobs (source, job_id, title, url) VALUES (?, ?, ?, ?)",
            ("crowdworks", "1", "既存案件", "https://example.com/1"),
        )
        jobs = [_make_job(job_id="1"), _make_job(job_id="2")]
        result = _exclude_existing(jobs, conn)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["job_id"], "2")

    def test_same_job_id_different_source(self):
        conn = _make_conn()
        conn.execute(
            "INSERT INTO jobs (source, job_id, title, url) VALUES (?, ?, ?, ?)",
            ("crowdworks", "1", "CW案件", "https://example.com/1"),
        )
        jobs = [
            _make_job(source="crowdworks", job_id="1"),
            _make_job(source="coconala", job_id="1"),
        ]
        result = _exclude_existing(jobs, conn)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source"], "coconala")


class TestMatchKeywords(unittest.TestCase):
    """キーワードマッチのテスト。"""

    def test_title_match(self):
        jobs = [_make_job(title="Python 開発案件", description="")]
        result = _match_keywords(jobs)
        self.assertEqual(len(result), 1)
        self.assertIn("Python", result[0]["matched_keywords"])

    def test_description_match(self):
        jobs = [_make_job(title="Webツール開発", description="AWS環境でのAPI構築")]
        result = _match_keywords(jobs)
        self.assertEqual(len(result), 1)
        self.assertIn("API", result[0]["matched_keywords"])

    def test_no_match(self):
        jobs = [_make_job(title="ライティング案件", description="記事を書く仕事です")]
        result = _match_keywords(jobs)
        self.assertEqual(len(result), 0)

    def test_multiple_keywords(self):
        jobs = [_make_job(title="Python + AI チャットボット", description="LLMを活用")]
        result = _match_keywords(jobs)
        self.assertEqual(len(result), 1)
        kw = result[0]["matched_keywords"]
        self.assertIn("Python", kw)
        self.assertIn("AI", kw)

    def test_case_insensitive(self):
        jobs = [_make_job(title="python script作成", description="")]
        result = _match_keywords(jobs)
        self.assertEqual(len(result), 1)


class TestExcludeByKeywords(unittest.TestCase):
    """除外キーワードのテスト。"""

    def test_no_exclusion(self):
        jobs = [_make_job()]
        result = _exclude_by_keywords(jobs)
        self.assertEqual(len(result), 1)

    def test_excluded_in_title(self):
        # _EXCLUDE_KW はモジュールレベル変数なので直接テストする場合は
        # パッチが必要だが、関数自体のロジックを検証
        import src.job_filter as jf
        original = jf._EXCLUDE_KW
        try:
            jf._EXCLUDE_KW = ["テスト除外"]
            jobs = [
                _make_job(title="テスト除外ワードを含む案件", job_id="1"),
                _make_job(title="正常な案件", job_id="2"),
            ]
            result = jf._exclude_by_keywords(jobs)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["job_id"], "2")
        finally:
            jf._EXCLUDE_KW = original


class TestFilterNewJobs(unittest.TestCase):
    """filter_new_jobs 統合テスト。"""

    def test_full_pipeline(self):
        conn = _make_conn()
        # 既存案件を1つ登録
        conn.execute(
            "INSERT INTO jobs (source, job_id, title, url) VALUES (?, ?, ?, ?)",
            ("crowdworks", "99", "既存", "https://example.com/99"),
        )
        jobs = [
            _make_job(job_id="99", title="Python既存案件"),  # 重複 → 除外
            _make_job(job_id="100", title="Python スクレイピング案件"),  # 新規+マッチ
            _make_job(job_id="101", title="記事作成", description="ブログ執筆"),  # マッチなし
            _make_job(job_id="102", title="ab", description="Python"),  # タイトル短い
        ]
        result = filter_new_jobs(jobs, conn)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["job_id"], "100")
        self.assertIn("Python", result[0]["matched_keywords"])

    def test_empty_input(self):
        conn = _make_conn()
        result = filter_new_jobs([], conn)
        self.assertEqual(result, [])

    def test_all_new_with_keywords(self):
        conn = _make_conn()
        jobs = [
            _make_job(job_id="1", title="AI チャットボット開発"),
            _make_job(job_id="2", title="機械学習モデル構築"),
        ]
        result = filter_new_jobs(jobs, conn)
        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
