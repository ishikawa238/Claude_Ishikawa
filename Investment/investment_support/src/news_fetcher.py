"""
ニュース取得モジュール

Google News RSS を利用して市場関連ニュース・銘柄別ニュースのヘッドラインを取得する。
バッチ実行時に呼び出され、LLM アドバイザーのプロンプトに含められる。

追加ライブラリ不要（requests + xml.etree.ElementTree）。
"""

import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import quote

import requests

from src.logger import get_logger

logger = get_logger(__name__)

_TIMEOUT_SEC = 10

# Google News RSS の検索キーワード（市場全体）
_MARKET_QUERIES: dict[str, list[str]] = {
    "JP": [
        "日経平均 OR 日本株 OR 東証",
        "株式市場 日本 決算",
    ],
    "US": [
        "S&P500 OR NASDAQ OR US stock market",
        "Fed OR earnings Wall Street",
    ],
}


# ============================================================
# 公開 API
# ============================================================


def fetch_market_news(market: str = "JP", limit: int = 5) -> list[dict]:
    """
    Google News RSS から市場関連ニュースヘッドラインを取得する。

    Args:
        market: "JP" or "US"
        limit: 取得する最大件数

    Returns:
        [{"title": "...", "published": "2026-05-08", "source": "..."}, ...]
        取得失敗時は空リスト。
    """
    queries = _MARKET_QUERIES.get(market, _MARKET_QUERIES["JP"])
    all_items: list[dict] = []

    for query in queries:
        items = _fetch_google_news_rss(query, market=market)
        all_items.extend(items)

    # 日付降順で重複除去（タイトルベース）
    seen_titles: set[str] = set()
    unique: list[dict] = []
    for item in sorted(all_items, key=lambda x: x.get("published", ""), reverse=True):
        if item["title"] not in seen_titles:
            seen_titles.add(item["title"])
            unique.append(item)

    result = unique[:limit]
    logger.info(f"市場ニュース取得: {market} {len(result)}件")
    return result


def fetch_ticker_news(
    tickers_with_names: list[tuple[str, str]],
    limit_per_ticker: int = 2,
) -> list[dict]:
    """
    特定銘柄のニュースを企業名で検索して取得する。

    Args:
        tickers_with_names: [(ticker, name), ...] 例: [("7203", "トヨタ自動車")]
        limit_per_ticker: 1銘柄あたりの最大件数

    Returns:
        [{"ticker": "7203", "title": "...", "published": "...", "source": "..."}, ...]
        取得失敗時は空リスト。
    """
    all_items: list[dict] = []

    for ticker, name in tickers_with_names:
        if not name or name == ticker:
            continue

        # 企業名の短縮形で検索（「株式会社」等を除去）
        search_name = _clean_company_name(name)
        if not search_name:
            continue

        items = _fetch_google_news_rss(search_name, market="JP")
        for item in items[:limit_per_ticker]:
            item["ticker"] = ticker
            all_items.append(item)

    logger.info(f"銘柄ニュース取得: {len(all_items)}件（{len(tickers_with_names)}銘柄）")
    return all_items


# ============================================================
# 内部ヘルパー
# ============================================================


def _fetch_google_news_rss(
    query: str,
    market: str = "JP",
    max_items: int = 10,
) -> list[dict]:
    """Google News RSS を検索クエリで取得してパースする。"""
    if market == "JP":
        hl, gl, ceid = "ja", "JP", "JP:ja"
    else:
        hl, gl, ceid = "en", "US", "US:en"

    url = (
        f"https://news.google.com/rss/search"
        f"?q={quote(query)}&hl={hl}&gl={gl}&ceid={ceid}"
    )

    try:
        resp = requests.get(url, timeout=_TIMEOUT_SEC)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"Google News RSS 取得失敗: {e}")
        return []

    return _parse_rss(resp.text, max_items)


def _parse_rss(xml_text: str, max_items: int = 10) -> list[dict]:
    """RSS XML をパースしてニュースアイテムのリストを返す。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning(f"RSS XMLパースエラー: {e}")
        return []

    items: list[dict] = []
    for item_el in root.iter("item"):
        if len(items) >= max_items:
            break

        title = _get_text(item_el, "title")
        if not title:
            continue

        pub_date_str = _get_text(item_el, "pubDate")
        published = _parse_pub_date(pub_date_str)

        # ソース名の抽出
        source = _get_text(item_el, "source") or ""

        # Google News はタイトルに「 - ソース名」を付加するので分離する
        if " - " in title:
            parts = title.rsplit(" - ", 1)
            title = parts[0].strip()
            if not source:
                source = parts[1].strip()

        items.append({
            "title": title,
            "published": published,
            "source": source,
        })

    return items


def _get_text(element: ET.Element, tag: str) -> Optional[str]:
    """子要素のテキストを取得する。"""
    child = element.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    return None


def _parse_pub_date(date_str: Optional[str]) -> str:
    """RSS の pubDate を YYYY-MM-DD に変換する。"""
    if not date_str:
        return ""
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return ""


def _clean_company_name(name: str) -> str:
    """検索用に企業名を短縮する。"""
    # 「株式会社」「(株)」「Inc.」等を除去
    removals = ["株式会社", "（株）", "(株)", "Inc.", "Corp.", "Ltd.", "Co.,"]
    result = name
    for r in removals:
        result = result.replace(r, "")
    return result.strip()
