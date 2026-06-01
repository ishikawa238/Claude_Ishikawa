"""
Screening ページ用の銘柄ブックマーク（screening_bookmarks.csv）
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from src.logger import get_logger

logger = get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_BOOKMARKS_CSV = _PROJECT_ROOT / "screening_bookmarks.csv"
_CSV_FIELDS = ["ticker", "name", "market", "memo", "added_at"]


def _ensure_csv() -> None:
    if not _BOOKMARKS_CSV.exists():
        with open(_BOOKMARKS_CSV, "w", encoding="utf-8", newline="") as f:
            csv.DictWriter(f, fieldnames=_CSV_FIELDS).writeheader()
        logger.info(f"screening_bookmarks.csv を作成しました: {_BOOKMARKS_CSV}")


def load_bookmarks() -> list[dict]:
    """ブックマーク一覧（added_at 昇順）。"""
    _ensure_csv()
    rows: list[dict] = []
    with open(_BOOKMARKS_CSV, encoding="utf-8") as f:
        for line in csv.DictReader(f):
            ticker = (line.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            rows.append({
                "ticker": ticker,
                "name": (line.get("name") or "").strip() or None,
                "market": (line.get("market") or "").strip().upper() or None,
                "memo": (line.get("memo") or "").strip() or None,
                "added_at": (line.get("added_at") or "").strip() or None,
            })
    return rows


def get_bookmark_tickers() -> set[str]:
    return {r["ticker"] for r in load_bookmarks()}


def is_bookmarked(ticker: str) -> bool:
    t = (ticker or "").strip().upper()
    return t in get_bookmark_tickers() if t else False


def toggle_bookmark(
    ticker: str,
    *,
    name: str | None = None,
    market: str | None = None,
) -> dict:
    """
    ブックマークの ON/OFF を切り替える。

    返り値: {bookmarked: bool, tickers: list[str], count: int}
    """
    t = (ticker or "").strip().upper()
    if not t:
        raise ValueError("ticker が必要です")

    rows = load_bookmarks()
    by_ticker = {r["ticker"]: r for r in rows}
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if t in by_ticker:
        del by_ticker[t]
        bookmarked = False
        logger.info(f"ブックマーク解除: {t}")
    else:
        by_ticker[t] = {
            "ticker": t,
            "name": (name or "").strip() or None,
            "market": (market or "").strip().upper() or None,
            "memo": None,
            "added_at": now,
        }
        bookmarked = True
        logger.info(f"ブックマーク追加: {t}")

    out = sorted(by_ticker.values(), key=lambda r: r.get("added_at") or "")
    with open(_BOOKMARKS_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for row in out:
            writer.writerow({
                "ticker": row["ticker"],
                "name": row.get("name") or "",
                "market": row.get("market") or "",
                "memo": row.get("memo") or "",
                "added_at": row.get("added_at") or "",
            })

    tickers = [r["ticker"] for r in out]
    return {"bookmarked": bookmarked, "tickers": tickers, "count": len(tickers)}
