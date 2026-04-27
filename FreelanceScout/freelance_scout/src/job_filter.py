"""
案件フィルタモジュール

スクレイプ済み案件に対して以下の処理を行う:
1. DB に既出の job_id を除外（重複排除）
2. キーワードマッチ（タイトル or 説明に1つ以上含む）
3. 除外キーワードフィルタ
4. タイトル最小文字数チェック
5. matched_keywords フィールドを付与
"""

import sqlite3

from src.config import CONFIG
from src.logger import get_logger

logger = get_logger(__name__)

_FILTER = CONFIG.get("filter", {})
_EXCLUDE_KW = [kw.lower() for kw in _FILTER.get("exclude_keywords", [])]
_MIN_TITLE_LEN = int(_FILTER.get("min_title_length", 5))

# キーワードは CrowdWorks / Coconala 両方の設定をマージして使う
_CW_KW = CONFIG.get("crowdworks", {}).get("keywords", [])
_CN_KW = CONFIG.get("coconala", {}).get("keywords", [])
_KEYWORDS = list({kw.lower(): kw for kw in _CW_KW + _CN_KW}.values())


def filter_new_jobs(
    all_jobs: list[dict],
    conn: sqlite3.Connection,
) -> list[dict]:
    """
    全取得案件から新着かつキーワードマッチする案件を返す。

    Args:
        all_jobs: スクレイパーが返した案件リスト
        conn: SQLite 接続（既存 job_id の検索に使用）

    Returns:
        フィルタ済み案件リスト（matched_keywords フィールド付き）
    """
    if not all_jobs:
        return []

    # 1. 重複排除: DB に既存の (source, job_id) を取得
    new_jobs = _exclude_existing(all_jobs, conn)
    logger.info(f"重複排除: {len(all_jobs)} -> {len(new_jobs)} 件")

    # 2. タイトル最小文字数チェック
    new_jobs = [j for j in new_jobs if len(j.get("title", "")) >= _MIN_TITLE_LEN]

    # 3. キーワードマッチ + matched_keywords 付与
    matched = _match_keywords(new_jobs)
    logger.info(f"キーワードマッチ: {len(matched)} 件")

    # 4. 除外キーワードフィルタ
    if _EXCLUDE_KW:
        before = len(matched)
        matched = _exclude_by_keywords(matched)
        logger.info(f"除外キーワード: {before} -> {len(matched)} 件")

    return matched


def _exclude_existing(
    jobs: list[dict], conn: sqlite3.Connection
) -> list[dict]:
    """DB に既存の案件を除外する。"""
    existing = set()
    rows = conn.execute("SELECT source, job_id FROM jobs").fetchall()
    for row in rows:
        existing.add((row["source"], row["job_id"]))

    return [
        j for j in jobs
        if (j["source"], j["job_id"]) not in existing
    ]


def _match_keywords(jobs: list[dict]) -> list[dict]:
    """キーワードマッチし、matched_keywords を付与した案件を返す。"""
    if not _KEYWORDS:
        # キーワード未設定時は全件通過（matched_keywords は空）
        for j in jobs:
            j["matched_keywords"] = ""
        return jobs

    result = []
    for job in jobs:
        text = (job.get("title", "") + " " + job.get("description", "")).lower()
        matched = [kw for kw in _KEYWORDS if kw.lower() in text]
        if matched:
            job["matched_keywords"] = ", ".join(matched)
            result.append(job)
    return result


def _exclude_by_keywords(jobs: list[dict]) -> list[dict]:
    """除外キーワードに該当する案件を除外する。"""
    result = []
    for job in jobs:
        text = (job.get("title", "") + " " + job.get("description", "")).lower()
        if not any(kw in text for kw in _EXCLUDE_KW):
            result.append(job)
    return result
