"""
CrowdWorks スクレイパー

crowdworks.jp の公開案件検索ページからJSON埋め込みデータを取得する。
ページは Vue.js SPA だが、初期データが div#vue-container の data 属性に
JSON として埋め込まれているため、requests + BeautifulSoup で取得可能。
"""

import json
import re
import time

import requests
from bs4 import BeautifulSoup

from src.config import CONFIG
from src.logger import get_logger

logger = get_logger(__name__)

_SCRAPING = CONFIG.get("scraping", {})
_CW = CONFIG.get("crowdworks", {})
_INTERVAL = float(_SCRAPING.get("request_interval_sec", 3))
_TIMEOUT = int(_SCRAPING.get("request_timeout_sec", 15))
_MAX_PAGES = int(_SCRAPING.get("max_pages_per_category", 3))
_USER_AGENT = _SCRAPING.get(
    "user_agent",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
)
_BASE_URL = _CW.get("base_url", "https://crowdworks.jp/public/jobs/search")


def fetch_jobs() -> list[dict]:
    """
    CrowdWorks からキーワード検索で案件を取得する。

    返り値: list[dict] — 各要素は以下のキーを持つ:
        source, job_id, title, url, description, budget, category, posted_date
    """
    if not _CW.get("enabled", True):
        logger.info("CrowdWorks: 無効化されています。スキップします。")
        return []

    keywords = _CW.get("keywords", [])
    if not keywords:
        logger.warning("CrowdWorks: キーワードが設定されていません。")
        return []

    all_jobs: dict[str, dict] = {}  # job_id -> job dict（重複排除）

    for kw in keywords:
        try:
            jobs = _search_by_keyword(kw)
            for job in jobs:
                all_jobs[job["job_id"]] = job
        except Exception as e:
            logger.error(f"CrowdWorks: キーワード '{kw}' の検索に失敗: {e}")

    result = list(all_jobs.values())
    logger.info(f"CrowdWorks: {len(result)} 件取得（キーワード {len(keywords)} 個）")
    return result


def _search_by_keyword(keyword: str) -> list[dict]:
    """キーワード1つで検索し、ページネーション分を取得する。"""
    jobs = []
    for page in range(1, _MAX_PAGES + 1):
        params = {
            "search[keywords]": keyword,
            "order": "new",
            "hide_expired": "true",
            "page": str(page),
        }
        page_jobs, total_page = _fetch_page(params)
        jobs.extend(page_jobs)

        if page >= total_page:
            break
        time.sleep(_INTERVAL)

    return jobs


def _fetch_page(params: dict) -> tuple[list[dict], int]:
    """
    1ページ分のHTMLを取得し、JSON埋め込みデータをパースする。
    返り値: (案件リスト, 総ページ数)
    """
    headers = {"User-Agent": _USER_AGENT}

    resp = requests.get(_BASE_URL, params=params, headers=headers, timeout=_TIMEOUT)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    vue = soup.find("div", id="vue-container")
    if not vue or not vue.get("data"):
        logger.warning("CrowdWorks: vue-container が見つかりません。")
        return [], 1

    data = json.loads(vue["data"])
    search_result = data.get("searchResult", {})
    job_offers = search_result.get("job_offers", [])

    page_info = search_result.get("page", {})
    total_page = page_info.get("total_page", 1)

    jobs = []
    for offer in job_offers:
        job = _parse_job_offer(offer)
        if job:
            jobs.append(job)

    if not jobs and job_offers:
        logger.warning(
            f"CrowdWorks: {len(job_offers)} 件の生データがあるがパースできませんでした。"
        )

    return jobs, total_page


def _parse_job_offer(offer: dict) -> dict | None:
    """JSON の1案件データを共通フォーマットに変換する。"""
    jo = offer.get("job_offer", {})
    job_id = jo.get("id")
    title = jo.get("title", "")

    if not job_id or not title:
        return None

    payment = offer.get("payment", {})
    budget = _format_budget(payment)

    return {
        "source": "crowdworks",
        "job_id": str(job_id),
        "title": title.strip(),
        "url": f"https://crowdworks.jp/public/jobs/{job_id}",
        "description": (jo.get("description_digest") or "")[:500],
        "budget": budget,
        "category": str(jo.get("category_id", "")),
        "posted_date": jo.get("last_released_at", ""),
    }


def _format_budget(payment: dict) -> str:
    """支払い情報を表示用文字列に変換する。"""
    if "fixed_price_payment" in payment:
        fp = payment["fixed_price_payment"]
        lo = fp.get("min_budget")
        hi = fp.get("max_budget")
        if lo is not None and hi is not None:
            return f"{int(lo):,}〜{int(hi):,}円"
        elif hi is not None:
            return f"〜{int(hi):,}円"
        elif lo is not None:
            return f"{int(lo):,}円〜"
    elif "hourly_payment" in payment:
        hp = payment["hourly_payment"]
        lo = hp.get("min_hourly_wage")
        hi = hp.get("max_hourly_wage")
        if lo is not None and hi is not None:
            return f"{int(lo):,}〜{int(hi):,}円/時"
        elif hi is not None:
            return f"〜{int(hi):,}円/時"
    return ""
