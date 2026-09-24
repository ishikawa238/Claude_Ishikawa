"""
yfinance 財務情報取得モジュール（投資委員会のファンダメンタル・アナリスト用）

yfinance の Ticker.info から主要な財務指標だけを抜き出し、
fundamentals_cache テーブルに 1 銘柄 1 日 1 回キャッシュする。
取得に失敗した場合は直近のキャッシュを返し、stale=True を付ける。

yfinance は非公式 API のため、項目の欠損・仕様変更がありうる。
"""

import json
from datetime import date

from src.data_store import fetch_one, get_connection, upsert
from src.logger import get_logger

logger = get_logger(__name__)

# yfinance info のキー → 委員会に渡す表示名
FUNDAMENTAL_FIELDS = {
    "trailingPE": "PER(実績)",
    "forwardPE": "PER(予想)",
    "priceToBook": "PBR",
    "returnOnEquity": "ROE",
    "revenueGrowth": "売上成長率(前年比)",
    "earningsGrowth": "利益成長率(前年比)",
    "operatingMargins": "営業利益率",
    "debtToEquity": "D/Eレシオ(%)",
    "dividendYield": "配当利回り",
    "marketCap": "時価総額",
    "sector": "セクター",
    "industry": "業種",
}


def _yf_symbol(ticker: str, market: str) -> str:
    return f"{ticker}.T" if market == "JP" else ticker


def _fetch_info_yf(symbol: str) -> dict:
    """yfinance から info を取得する（テストではモックする）。"""
    import yfinance as yf

    return yf.Ticker(symbol).info or {}


def _extract_fields(info: dict) -> dict:
    return {key: info[key] for key in FUNDAMENTAL_FIELDS if info.get(key) is not None}


def get_fundamentals(ticker: str, market: str, today: date | None = None) -> dict:
    """
    財務指標を返す。

    返り値:
        {"data": {...}, "fetched_date": "YYYY-MM-DD" | None, "stale": bool}
        data が空の場合は取得もキャッシュも無い。
    """
    today_str = (today or date.today()).strftime("%Y-%m-%d")

    try:
        with get_connection() as conn:
            cached = fetch_one(
                conn,
                "SELECT fetched_date, data_json FROM fundamentals_cache "
                "WHERE ticker = ? ORDER BY fetched_date DESC LIMIT 1",
                (ticker,),
            )
    except Exception as e:
        logger.warning(f"fundamentals_cache 読み込み失敗 ({ticker}): {e}")
        cached = None

    if cached and cached["fetched_date"] == today_str:
        return {"data": json.loads(cached["data_json"]), "fetched_date": today_str, "stale": False}

    try:
        data = _extract_fields(_fetch_info_yf(_yf_symbol(ticker, market)))
    except Exception as e:
        logger.warning(f"yfinance 財務取得失敗 ({ticker}): {e}")
        data = {}

    if data:
        try:
            with get_connection() as conn:
                upsert(conn, "fundamentals_cache", [{
                    "ticker": ticker,
                    "fetched_date": today_str,
                    "data_json": json.dumps(data, ensure_ascii=False),
                }])
        except Exception as e:
            logger.warning(f"fundamentals_cache 保存失敗 ({ticker}): {e}")
        return {"data": data, "fetched_date": today_str, "stale": False}

    if cached:
        return {"data": json.loads(cached["data_json"]), "fetched_date": cached["fetched_date"], "stale": True}
    return {"data": {}, "fetched_date": None, "stale": True}
