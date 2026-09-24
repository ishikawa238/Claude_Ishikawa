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
_EODHD_FUNDAMENTALS_DISABLED = False
# false の場合は HTTP を送らず即座に失敗扱いにし、呼び出し元の yfinance フォールバックに任せる
_ENABLED = str(CONFIG["eodhd"].get("enabled", True)).lower() not in ("false", "0", "no")
_disabled_logged = False

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
    if not _ENABLED:
        global _disabled_logged
        if not _disabled_logged:
            logger.info("EODHD は無効（eodhd.enabled=false）のため呼び出しをスキップします")
            _disabled_logged = True
        return None

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
            elif response.status_code == 403 and "/fundamentals/" in url:
                global _EODHD_FUNDAMENTALS_DISABLED
                if not _EODHD_FUNDAMENTALS_DISABLED:
                    logger.info(
                        "EODHD fundamentals API は現在のプランでは利用できません。"
                        "US 発行済株式数の自動取得をスキップします。"
                        "（allow_unknown_market_cap=true で JP スクリーニングは継続可能）"
                    )
                    _EODHD_FUNDAMENTALS_DISABLED = True
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
        name = (r.get("Name") or "").strip()
        asset_type = (r.get("Type") or "").lower()
        exchange_detail = " ".join(
            str(r.get(key) or "")
            for key in ("Exchange", "ExchangeCode", "ExchangeName")
        ).strip()

        is_excluded, exclude_reason = _check_excluded(
            asset_type,
            name=name,
            exchange_detail=exchange_detail,
        )

        sector_raw = (r.get("Sector") or "").strip() or None
        industry_raw = (r.get("Industry") or "").strip() or None

        rows.append({
            "ticker":             ticker,
            "name":               name,
            "market":             market,
            "exchange":           exchange,
            "shares_outstanding": None,
            "is_active":          1,
            "is_excluded":        1 if is_excluded else 0,
            "exclude_reason":     exclude_reason,
            "sector":             sector_raw,
            "industry":           industry_raw,
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
_JPX_COL_CODE     = "コード"
_JPX_COL_NAME     = "銘柄名"
_JPX_COL_SEGMENT  = "市場・商品区分"
_JPX_COL_SECTOR33 = "33業種区分"
_JPX_COL_SECTOR17 = "17業種区分"


def _fetch_jp_symbols_from_jpx() -> list[dict]:
    """
    JPX が公開する上場銘柄一覧 Excel から JP 銘柄マスタを取得する。
    EODHD exchange-symbol-list の JP 代替。

    NOTE: JPX の .xls は BIFF8 形式だが codepage 宣言が不正（1200=UTF-16LE）なため
    xlrd が日本語列名を文字化けさせる。列名ではなく列インデックスで参照する。
    列順: 0=日付, 1=コード, 2=銘柄名, 3=市場・商品区分, 4=33業種コード,
          5=33業種区分, 6=17業種コード, 7=17業種区分, 8=規模コード, 9=規模区分
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
            header=None,
            skiprows=1,
            dtype={1: str},
            engine="xlrd",
        )
    except Exception as e:
        logger.error(f"JPX Excel パース失敗: {e}")
        return []

    if len(df.columns) < 8:
        logger.error(
            f"JPX Excel の列数が想定と異なります（{len(df.columns)}列）。"
            "フォーマット変更の可能性があります。"
        )
        return []

    today = date.today().strftime("%Y-%m-%d")
    rows = []
    for _, r in df.iterrows():
        code = str(r.iloc[1]).strip()
        if not code or not code.isdigit():
            continue

        segment = str(r.iloc[3]).lower()
        is_excluded, exclude_reason = _classify_jpx_segment(segment)

        name_raw = str(r.iloc[2]).strip()
        sector33 = str(r.iloc[5]).strip()
        sector17 = str(r.iloc[7]).strip()

        rows.append({
            "ticker":             code,
            "name":               name_raw,
            "market":             "JP",
            "exchange":           "TSE",
            "shares_outstanding": None,
            "is_active":          1,
            "is_excluded":        1 if is_excluded else 0,
            "exclude_reason":     exclude_reason,
            "sector":             sector33 if sector33 and sector33 != "-" else None,
            "industry":           sector17 if sector17 and sector17 != "-" else None,
            "last_updated":       today,
        })

    logger.info(f"JPX 銘柄マスタ取得完了: {len(rows)} 銘柄")
    return rows


def _classify_jpx_segment(segment: str) -> tuple[bool, Optional[str]]:
    """
    JPX の市場・商品区分文字列から除外判定を行う。
    返り値: (is_excluded: bool, reason: str or None)

    NOTE: JPX Excel の文字化けにより日本語文字列での照合は不可。
    半角英字キーワード (etf, etn, reit, pro market) で判定する。
    """
    if "etf" in segment or "etn" in segment:
        return True, "ETF"
    if "reit" in segment:
        return True, "REIT"
    if "pro market" in segment:
        return True, "PRO Market"
    return False, None


def _check_excluded(
    asset_type: str,
    *,
    name: str = "",
    exchange_detail: str = "",
) -> tuple[bool, Optional[str]]:
    """
    資産タイプが対象外かどうかを判定する。
    返り値: (is_excluded: bool, reason: str or None)
    """
    normalized_exchange = exchange_detail.lower()
    if any(key in normalized_exchange for key in ("otc", "pink", "grey", "expert market")):
        return True, "OTC"

    excluded_types = {
        "etf":             "ETF",
        "fund":            "投資信託",
        "reit":            "REIT",
        "preferred":       "優先株",
        "warrant":         "ワラント",
        "right":           "権利",
        "unit":            "ユニット",
        "bond":            "債券",
        "note":            "債券",
        "option":          "オプション",
        "certificate":     "証書",
    }
    for key, reason in excluded_types.items():
        if key in asset_type:
            return True, reason

    normalized_name = name.lower().strip()
    if "liquidation" in normalized_name or "liquidating" in normalized_name:
        return True, "清算銘柄"

    excluded_name_suffixes = {
        " warrant": "ワラント",
        " warrants": "ワラント",
        " right": "権利",
        " rights": "権利",
        " unit": "ユニット",
        " units": "ユニット",
    }
    for suffix, reason in excluded_name_suffixes.items():
        if normalized_name.endswith(suffix):
            return True, reason
    return False, None


# ============================================================
# 発行済株式数の取得
# ============================================================

def is_eodhd_fundamentals_disabled() -> bool:
    """fundamentals API がプラン制限で使えない場合 True。"""
    return _EODHD_FUNDAMENTALS_DISABLED


def fetch_shares_outstanding(ticker: str, market: str) -> Optional[float]:
    """
    EODHD の fundamental API から発行済株式数を取得する。
    取得できない場合は None を返す。

    注意: fundamental API は EODHD のプランによっては利用不可の場合がある。
    取得不可の場合は時価総額フィルタを無効化して運用する。
    """
    if _EODHD_FUNDAMENTALS_DISABLED:
        return None
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

    url = f"{_BASE_URL}/eod-bulk-last-day/{exchange}"
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
