"""
Coconala スクレイパー

coconala.com の公開依頼（リクエスト）カテゴリページから案件を取得する。
サーバーサイドレンダリングの HTML を BeautifulSoup でパースする。
"""

import re
import time

import requests
from bs4 import BeautifulSoup

from src.config import CONFIG
from src.logger import get_logger

logger = get_logger(__name__)

_SCRAPING = CONFIG.get("scraping", {})
_CN = CONFIG.get("coconala", {})
_INTERVAL = float(_SCRAPING.get("request_interval_sec", 3))
_TIMEOUT = int(_SCRAPING.get("request_timeout_sec", 15))
_MAX_PAGES = int(_SCRAPING.get("max_pages_per_category", 3))
_USER_AGENT = _SCRAPING.get(
    "user_agent",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
)
_BASE_URL = _CN.get("base_url", "https://coconala.com/requests/categories")

# CSS セレクタ定数（サイト変更時はここを修正）
_SEL_ITEM = "div.c-searchItemWrapper"
_SEL_TITLE = "div.c-itemInfo_title > a"
_SEL_DESCRIPTION = "div.c-itemInfo_description"
_SEL_BUDGET = "span.d-requestBudget_emphasis"
_SEL_CATEGORY = "a.c-itemInfo_category"
_SEL_POSTED_DATE = "div.c-itemInfoUser_created span[title]"


def fetch_jobs() -> list[dict]:
    """
    Coconala のカテゴリ別に公開依頼を取得する。

    返り値: list[dict] — 各要素は以下のキーを持つ:
        source, job_id, title, url, description, budget, category, posted_date
    """
    if not _CN.get("enabled", True):
        logger.info("Coconala: 無効化されています。スキップします。")
        return []

    category_ids = _CN.get("category_ids", [])
    if not category_ids:
        logger.warning("Coconala: カテゴリIDが設定されていません。")
        return []

    all_jobs: dict[str, dict] = {}  # job_id -> job dict（重複排除）

    for cat_id in category_ids:
        try:
            jobs = _fetch_category(cat_id)
            for job in jobs:
                all_jobs[job["job_id"]] = job
        except Exception as e:
            logger.error(f"Coconala: カテゴリ {cat_id} の取得に失敗: {e}")

    result = list(all_jobs.values())
    logger.info(f"Coconala: {len(result)} 件取得（カテゴリ {len(category_ids)} 個）")
    return result


def _fetch_category(category_id: int) -> list[dict]:
    """カテゴリ1つにつき、ページネーション分を取得する。"""
    jobs = []
    for page in range(1, _MAX_PAGES + 1):
        url = f"{_BASE_URL}/{category_id}?page={page}"
        page_jobs = _fetch_page(url)
        if not page_jobs:
            break
        jobs.extend(page_jobs)
        time.sleep(_INTERVAL)

    return jobs


def _fetch_page(url: str) -> list[dict]:
    """1ページ分のHTMLを取得し、案件リストをパースする。"""
    headers = {"User-Agent": _USER_AGENT}

    resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    items = soup.select(_SEL_ITEM)

    if not items:
        logger.debug(f"Coconala: 0 件（{url}）")
        return []

    jobs = []
    for item in items:
        job = _parse_item(item)
        if job:
            jobs.append(job)

    return jobs


def _parse_item(item) -> dict | None:
    """HTML の1案件要素を共通フォーマットに変換する。"""
    # タイトルとURL
    title_el = item.select_one(_SEL_TITLE)
    if not title_el:
        return None

    title = title_el.get_text(strip=True)
    href = title_el.get("href", "")

    # job_id をURLから抽出
    m = re.search(r"/requests/(\d+)", href)
    if not m:
        return None
    job_id = m.group(1)

    # URLを絶対パスに
    if href.startswith("/"):
        href = f"https://coconala.com{href}"

    # 説明文
    desc_el = item.select_one(_SEL_DESCRIPTION)
    description = desc_el.get_text(strip=True)[:500] if desc_el else ""

    # 予算
    budget_el = item.select_one(_SEL_BUDGET)
    budget = budget_el.get_text(strip=True) if budget_el else ""

    # カテゴリ
    cat_el = item.select_one(_SEL_CATEGORY)
    category = cat_el.get_text(strip=True) if cat_el else ""

    # 投稿日（title属性に完全な日付が入っている）
    date_el = item.select_one(_SEL_POSTED_DATE)
    posted_date = date_el.get("title", "") if date_el else ""

    return {
        "source": "coconala",
        "job_id": job_id,
        "title": title,
        "url": href,
        "description": description,
        "budget": budget,
        "category": category,
        "posted_date": posted_date,
    }
