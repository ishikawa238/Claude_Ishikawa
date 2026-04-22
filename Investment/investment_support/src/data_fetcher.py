"""
EODHDデータ取得モジュール

役割:
- EODHD Bulk EOD endpoint で取引所全銘柄の当日データを1リクエストで取得（日次バッチ用）
- 個別銘柄の履歴取得（初回セットアップ用・フォールバック用）
- 銘柄マスタ取得
- リトライ処理・エラーハンドリング

API コール数の設計:
- 日次バッチ: Bulk endpoint → JP 1回 + US 1回 + インデックス数本 = 約6回/日
- 初回履歴取得: yfinance_fetcher.py を使用（無料・API制限なし）
- EODHD 無料プラン（20回/日）で日次バッチが動作可能
"""

import io
import time
from datetime import date, datetime, timedelta
from typing import Optional

import requests

from src.config import CONFIG
from src.logger import get_logger

logger = get_logger(__name__)

# ============================================================
# 定数
# ============================================================

_BASE_URL = CONFIG["eodhd"]["base_url"].rstrip("/")
_API_KEY = CONFIG["eodhd"]["api_key"]
_INTERVAL = float(CONFIG["eodhd"].get("request_interval_sec", 0.5))
_MAX_RETRIES = 3
_RETRY_WAIT_SEC = 5

# 市場インデックスのティッカー定義
INDEX_TICKERS = {
    "N225":   "N225.INDX",      # 日経平均
    "TOPIX":  "TOPIX.INDX",    # TOPIX
    "SP500":  "GSPC.INDX",     # S&P500
    "NASDAQ": "IXIC.INDX",     # NASDAQ総合
}

# 取引所コード（銘柄マスタ取得に使用）
# EODHD では東証 = "JP"、米国 = "US"
EXCHANGE_CODES = {
    "JP": "JP",    # 東京証券取引所（EODHD での正式コード）
    "US": "US",    # 米国市場（NYSE + NASDAQ）
}


# ============================================================
# 内部ユーティリティ
# ============================================================

def _get(url: str, params: dict) -> Optional[list | dict]:
    """
    GET リクエストを送信する。失敗した場合は最大3回リトライする。
    返り値: JSONのリストまたは辞書。失敗時は None。
    """
    params["api_token"] = _API_KEY
    params["fmt"] = "json"

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            time.sleep(_INTERVAL)
            response = requests.get(url, params=params, timeout=30)

            if response.status_code == 200:
                return response.json()
            elif response.status_code == 429:
                wait = _RETRY_WAIT_SEC * attempt
                logger.warning(f"レートリミット到達。{wait}秒待機後リトライ ({attempt}/{_MAX_RETRIES})")
                time.sleep(wait)
            elif response.status_code == 404:
                # 銘柄が存在しない・廃止等
                return None
            else:
                logger.warning(
                    f"APIエラー: HTTP {response.status_code} | URL: {url} "
                    f"({attempt}/{_MAX_RETRIES})"
                )
                time.sleep(_RETRY_WAIT_SEC)

        except requests.exceptions.Timeout:
            logger.warning(f"タイムアウト ({attempt}/{_MAX_RETRIES}): {url}")
            time.sleep(_RETRY_WAIT_SEC)
        except requests.exceptions.ConnectionError:
            logger.warning(f"接続エラー ({attempt}/{_MAX_RETRIES}): {url}")
            time.sleep(_RETRY_WAIT_SEC)
        except Exception as e:
            logger.error(f"予期しないエラー: {e} | URL: {url}")
            return None

    logger.error(f"最大リトライ回数到達。スキップします: {url}")
    return None


def _eodhd_ticker(ticker: str, market: str) -> str:
    """
    内部ティッカーを EODHD 形式に変換する。
    例: JP + 7203 → 7203.T
        US + AAPL → AAPL.US
    """
    if market == "JP":
        return f"{ticker}.T"
    else:
        return f"{ticker}.US"


# ============================================================
# 価格データ取得
# ============================================================

def fetch_price_history(
    ticker: str,
    market: str,
    from_date: date,
    to_date: date,
) -> list[dict]:
    """
    指定銘柄の日足価格データを取得する。

    返り値: [
        {
            "date": "2025-01-15",
            "open": 3180.0,
            "high": 3210.0,
            "low": 3160.0,
            "close": 3200.0,
            "adjusted_close": 3200.0,
            "volume": 5230000.0,
        },
        ...
    ]
    失敗時は空リストを返す。
    """
    eodhd_tk = _eodhd_ticker(ticker, market)
    url = f"{_BASE_URL}/eod/{eodhd_tk}"
    params = {
        "from": from_date.strftime("%Y-%m-%d"),
        "to": to_date.strftime("%Y-%m-%d"),
    }

    raw = _get(url, params)
    if not raw or not isinstance(raw, list):
        return []

    rows = []
    for r in raw:
        try:
            rows.append({
                "ticker":     ticker,
                "date":       r["date"],
                "open":       float(r.get("open") or 0) or None,
                "high":       float(r.get("high") or 0) or None,
                "low":        float(r.get("low") or 0) or None,
                "close":      float(r.get("close") or 0) or None,
                "adj_close":  float(r.get("adjusted_close") or 0) or None,
                "volume":     float(r.get("volume") or 0) or None,
                "is_holiday": 0,
            })
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"価格データのパースに失敗: {ticker} {r.get('date')} ({e})")

    return rows


def fetch_index_history(
    index_code: str,
    from_date: date,
    to_date: date,
) -> list[dict]:
    """
    市場インデックスの日足価格データを取得する。
    index_code: N225 / TOPIX / SP500 / NASDAQ

    返り値: [{"index_code": ..., "date": ..., "close": ...}, ...]
    """
    eodhd_tk = INDEX_TICKERS.get(index_code)
    if not eodhd_tk:
        logger.error(f"未定義のインデックスコード: {index_code}")
        return []

    url = f"{_BASE_URL}/eod/{eodhd_tk}"
    params = {
        "from": from_date.strftime("%Y-%m-%d"),
        "to":   to_date.strftime("%Y-%m-%d"),
    }

    raw = _get(url, params)
    if not raw or not isinstance(raw, list):
        logger.warning(f"インデックスデータ取得失敗: {index_code}")
        return []

    rows = []
    for r in raw:
        try:
            close = float(r.get("adjusted_close") or r.get("close") or 0)
            if close == 0:
                continue
            rows.append({
                "index_code": index_code,
                "date":       r["date"],
                "close":      close,
                # MA は technical_calculator で別途計算するため None で初期化
                "ma25":        None,
                "ma75":        None,
                "ma200":       None,
                "ma25_5d_ago": None,
            })
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"インデックスデータのパースに失敗: {index_code} {r.get('date')} ({e})")

    return rows


# ============================================================
# 銘柄マスタ取得
# ============================================================

def fetch_exchange_symbols(market: str) -> list[dict]:
    """
    指定市場の銘柄一覧を取得する。
    JP: JPX 公開の上場銘柄一覧 Excel を使用（EODHD exchange-symbol-list は JP 非対応）
    US: EODHD exchange-symbol-list を使用

    返り値: [
        {
            "ticker": "7203",
            "name": "Toyota Motor",
            "market": "JP",
            "exchange": "TSE",
            "is_active": 1,
            "is_excluded": 0,
            "exclude_reason": None,
            "shares_outstanding": None,
            "last_updated": "2025-01-20",
        },
        ...
    ]
    """
    if market == "JP":
        return _fetch_jp_symbols_from_jpx()

    exchange = EXCHANGE_CODES.get(market)
    if not exchange:
        logger.error(f"未定義の市場コード: {market}")
        return []

    url = f"{_BASE_URL}/exchange-symbol-list/{exchange}"
    raw = _get(url, {})

    if not raw or not isinstance(raw, list):
        logger.warning(f"銘柄マスタ取得失敗: {market}")
        return []

    today = date.today().strftime("%Y-%m-%d")
    rows = []
    for r in raw:
        ticker_raw = r.get("Code", "")
        if not ticker_raw:
            continue

        ticker = ticker_raw.replace(".T", "").replace(".US", "").strip()
        asset_type = (r.get("Type") or "").lower()

        is_excluded, exclude_reason = _check_excluded(asset_type)

        rows.append({
            "ticker":             ticker,
            "name":               (r.get("Name") or "").strip(),
            "market":             market,
            "exchange":           exchange,
            "shares_outstanding": None,
            "is_active":          1,
            "is_excluded":        1 if is_excluded else 0,
            "exclude_reason":     exclude_reason,
            "last_updated":       today,
        })

    logger.info(f"銘柄マスタ取得完了: {market} {len(rows)}銘柄")
    return rows


# JPX 上場銘柄一覧の URL（毎営業日更新）
_JPX_SYMBOLS_URL = (
    "https://www.jpx.co.jp/markets/statistics-equities/misc/"
    "tvdivq0000001vg2-att/data_j.xls"
)

# JPX Excel の列名
_JPX_COL_CODE    = "コード"
_JPX_COL_NAME    = "銘柄名"
_JPX_COL_SEGMENT = "市場・商品区分"


def _fetch_jp_symbols_from_jpx() -> list[dict]:
    """
    JPX が公開する上場銘柄一覧 Excel から JP 銘柄マスタを取得する。
    EODHD exchange-symbol-list の JP 代替。
    """
    try:
        import pandas as pd
    except ImportError:
        logger.error("pandas がインストールされていません: pip install pandas openpyxl")
        return []

    logger.info("JPX 上場銘柄一覧を取得中...")
    try:
        resp = requests.get(_JPX_SYMBOLS_URL, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"JPX 銘柄一覧ダウンロード失敗: {e}")
        return []

    try:
        df = pd.read_excel(
            io.BytesIO(resp.content),
            dtype={_JPX_COL_CODE: str},
            engine="xlrd",
        )
    except Exception as e:
        logger.error(f"JPX Excel パース失敗: {e}")
        return []

    if _JPX_COL_CODE not in df.columns:
        logger.error(
            f"JPX Excel に '{_JPX_COL_CODE}' 列が見つかりません。"
            f"実際の列: {list(df.columns)}"
        )
        return []

    today = date.today().strftime("%Y-%m-%d")
    rows = []
    for _, r in df.iterrows():
        code = str(r.get(_JPX_COL_CODE, "")).strip()
        # 4桁数字のみを対象とする（ETF等は別途 segment で除外）
        if not code or not code.isdigit():
            continue

        segment = str(r.get(_JPX_COL_SEGMENT, "")).lower()
        is_excluded, exclude_reason = _classify_jpx_segment(segment)

        rows.append({
            "ticker":             code,
            "name":               str(r.get(_JPX_COL_NAME, "")).strip(),
            "market":             "JP",
            "exchange":           "TSE",
            "shares_outstanding": None,
            "is_active":          1,
            "is_excluded":        1 if is_excluded else 0,
            "exclude_reason":     exclude_reason,
            "last_updated":       today,
        })

    logger.info(f"JPX 銘柄マスタ取得完了: {len(rows)} 銘柄")
    return rows


def _classify_jpx_segment(segment: str) -> tuple[bool, Optional[str]]:
    """
    JPX の市場・商品区分文字列から除外判定を行う。
    返り値: (is_excluded: bool, reason: str or None)
    """
    if "etf" in segment or "etn" in segment:
        return True, "ETF"
    if "reit" in segment or "不動産投資信託" in segment:
        return True, "REIT"
    if "インフラファンド" in segment:
        return True, "インフラファンド"
    if "優先出資" in segment:
        return True, "優先株"
    return False, None


def _check_excluded(asset_type: str) -> tuple[bool, Optional[str]]:
    """
    資産タイプが対象外かどうかを判定する。
    返り値: (is_excluded: bool, reason: str or None)
    """
    excluded_types = {
        "etf":             "ETF",
        "fund":            "投資信託",
        "reit":            "REIT",
        "preferred stock": "優先株",
        "warrant":         "ワラント",
    }
    for key, reason in excluded_types.items():
        if key in asset_type:
            return True, reason
    return False, None


# ============================================================
# 発行済株式数の取得
# ============================================================

def fetch_shares_outstanding(ticker: str, market: str) -> Optional[float]:
    """
    EODHD の fundamental API から発行済株式数を取得する。
    取得できない場合は None を返す。

    注意: fundamental API は EODHD のプランによっては利用不可の場合がある。
    取得不可の場合は時価総額フィルタを無効化して運用する。
    """
    eodhd_tk = _eodhd_ticker(ticker, market)
    url = f"{_BASE_URL}/fundamentals/{eodhd_tk}"
    params = {"filter": "SharesStats::SharesOutstanding"}

    raw = _get(url, params)
    if not raw:
        return None

    try:
        # レスポンス形式: {"SharesStats": {"SharesOutstanding": 12345678}}
        # または filter パラメータ指定時は数値が直接返る場合もある
        if isinstance(raw, (int, float)):
            return float(raw)
        if isinstance(raw, dict):
            shares = raw.get("SharesStats", {}).get("SharesOutstanding")
            if shares:
                return float(shares)
    except (ValueError, TypeError, AttributeError):
        pass

    return None


# ============================================================
# Bulk EOD 取得（日次バッチ専用・1リクエストで取引所全銘柄）
# ============================================================

def fetch_bulk_eod(market: str, target_date: Optional[date] = None) -> list[dict]:
    """
    EODHD の Bulk EOD エンドポイントで取引所全銘柄の当日データを取得する。
    1リクエストで取引所全銘柄を取得できるため、API コール数を大幅に削減できる。

    引数:
        market: "JP" または "US"
        target_date: 取得対象日（省略時は最新営業日）

    返り値: price_history テーブルへの INSERT 用辞書リスト
    """
    exchange = EXCHANGE_CODES.get(market)
    if not exchange:
        logger.error(f"未定義の市場コード: {market}")
        return []

    url = f"{_BASE_URL}/eod/bulk_last_day/{exchange}"
    params: dict = {}
    if target_date:
        params["date"] = target_date.strftime("%Y-%m-%d")

    raw = _get(url, params)
    if not raw or not isinstance(raw, list):
        logger.warning(f"Bulk EOD 取得失敗: {market}")
        return []

    rows = []
    for r in raw:
        ticker_raw = (r.get("code") or r.get("Code") or "").strip()
        if not ticker_raw:
            continue
        # EODHD bulk は "7203.T" 形式で返ってくるので内部形式に変換
        ticker = ticker_raw.replace(".T", "").replace(".US", "").strip()

        try:
            adj = float(r.get("adjusted_close") or r.get("close") or 0)
            rows.append({
                "ticker":     ticker,
                "date":       r.get("date", (target_date or date.today()).strftime("%Y-%m-%d")),
                "open":       float(r.get("open") or 0) or None,
                "high":       float(r.get("high") or 0) or None,
                "low":        float(r.get("low") or 0) or None,
                "close":      float(r.get("close") or 0) or None,
                "adj_close":  adj or None,
                "volume":     float(r.get("volume") or 0) or None,
                "is_holiday": 0,
            })
        except (ValueError, TypeError) as e:
            logger.warning(f"Bulk EOD パース失敗: {ticker_raw} ({e})")

    logger.info(f"Bulk EOD 取得完了: {market} {len(rows)} 件")
    return rows


# ============================================================
# 個別銘柄一括取得（フォールバック・履歴補完用）
# ============================================================

def fetch_prices_bulk(
    tickers_with_market: list[tuple[str, str]],
    from_date: date,
    to_date: date,
    batch_type: str = "",
) -> tuple[list[dict], list[str]]:
    """
    複数銘柄の価格データを1件ずつ取得する。
    ※ 通常の日次バッチでは fetch_bulk_eod を使うこと。
    本関数は個別補完・テスト用。

    返り値: (成功した全行データのリスト, 失敗したティッカーのリスト)
    """
    from src.data_store import log_error

    all_rows: list[dict] = []
    failed_tickers: list[str] = []
    total = len(tickers_with_market)

    for i, (ticker, market) in enumerate(tickers_with_market, start=1):
        if i % 100 == 0:
            logger.info(f"  価格取得進捗: {i}/{total}")

        rows = fetch_price_history(ticker, market, from_date, to_date)
        if rows:
            all_rows.extend(rows)
        else:
            failed_tickers.append(ticker)
            log_error(
                batch_type=batch_type,
                module="data_fetcher",
                error_type="PriceFetchFailed",
                error_message=f"価格データ取得失敗: {ticker}",
                ticker=ticker,
                is_fatal=False,
            )

    logger.info(
        f"価格取得完了（個別）: 成功 {total - len(failed_tickers)} 件 / "
        f"失敗 {len(failed_tickers)} 件"
    )
    return all_rows, failed_tickers
