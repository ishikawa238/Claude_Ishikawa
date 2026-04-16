"""
J-Quants データ取得モジュール

役割:
- J-Quants API（fins/statements）から財務データを取得
- fundamentals_snapshot テーブルへの UPSERT
- universe_master.shares_outstanding の更新

対象市場: JP のみ（J-Quants は国内株専用）

API コール設計:
- 月次バッチで銘柄ごとに1リクエスト
- request_interval_sec（デフォルト1.0秒）でレートリミット回避

注意:
- config.json の jquants.api_key にリフレッシュトークンを設定すること
- PER / PBR / dividend_yield は price_history の最新終値から計算（API から直接取得しない）
- J-Quants フィールド名は API v2 仕様に基づく。変更時は _FIELD_MAP を修正する
"""

import time
from datetime import date
from typing import Optional

import jquantsapi

from src.config import CONFIG
from src.data_store import fetch_one, get_connection, upsert
from src.logger import get_logger

logger = get_logger(__name__)

# ============================================================
# 定数
# ============================================================

_REFRESH_TOKEN = CONFIG["jquants"]["api_key"]
_INTERVAL = float(CONFIG["jquants"].get("request_interval_sec", 1.0))

# J-Quants fins/statements フィールド名 → 内部カラム名のマッピング
# 実際の API レスポンスと齟齬がある場合はここを修正する
_FIELD_MAP = {
    "NetSales":                 "revenue",
    "OperatingProfit":          "operating_income",
    "OrdinaryProfit":           "ordinary_income",
    "Profit":                   "net_income",
    "EarningsPerShare":         "eps",
    "BookValuePerShare":        "bps",
    # 発行済株式数（自己株式含む期末残高）
    "NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYearIncludingTreasuryStock": "shares_outstanding",
    # 年間配当（1株当たり）
    "ResultDividendPerShareAnnual": "dividend_per_share",
}

# 会計期間種別フィールド（DataFrame 上のカラム名）
_PERIOD_TYPE_COL     = "TypeOfCurrentPeriod"
_FISCAL_YEAR_END_COL = "CurrentFiscalYearEndDate"  # datetime 型。.year で会計年度を取得
_DATE_COL            = "DisclosedDate"

# fins/announcement フィールド名（FINS_ANNOUNCEMENT_COLUMNS に準拠）
_ANN_DATE_COL    = "Date"
_ANN_PERIOD_COL  = "FiscalQuarter"
_ANN_YEAR_COL    = "FiscalYear"
_ANN_CODE_COL    = "Code"


# ============================================================
# 公開 API
# ============================================================

def fetch_and_save_fundamentals(tickers: list[str], today: date) -> int:
    """
    J-Quants から JP 銘柄の財務データを取得し DB に保存する。

    - fundamentals_snapshot に UPSERT（fiscal_year + fiscal_quarter が UNIQUE KEY）
    - universe_master.shares_outstanding を最新値で上書き
    - PER / PBR / dividend_yield は price_history の最新終値から計算

    引数:
        tickers: universe_master から取得した JP ティッカーのリスト
        today:   バッチ実行日（fetched_at / last_updated に使用）

    返り値: 正常に保存できた銘柄数
    """
    if not _REFRESH_TOKEN or _REFRESH_TOKEN == "YOUR_JQUANTS_API_KEY":
        logger.warning("J-Quants API キーが未設定です。財務データ取得をスキップします。")
        return 0

    client = _get_client()
    today_str = today.strftime("%Y-%m-%d")
    saved = 0
    total = len(tickers)

    for i, ticker in enumerate(tickers, start=1):
        if i % 100 == 0:
            logger.info(f"  財務データ取得進捗: {i}/{total}")

        try:
            time.sleep(_INTERVAL)
            row = _fetch_latest_statement(client, ticker, today_str)
            if row is None:
                continue

            with get_connection() as conn:
                upsert(conn, "fundamentals_snapshot", [row])
                if row.get("shares_outstanding") is not None:
                    conn.execute(
                        "UPDATE universe_master SET shares_outstanding=?, last_updated=? "
                        "WHERE ticker=?",
                        (row["shares_outstanding"], today_str, ticker),
                    )

            saved += 1

        except Exception as e:
            logger.warning(f"財務データ取得失敗: {ticker} ({e})")

    logger.info(f"財務データ更新完了: {saved}/{total} 銘柄")
    return saved


def fetch_and_save_earnings_schedule(tickers: list[str], today: date) -> int:
    """
    保有銘柄の決算発表予定日を J-Quants から取得し earnings_schedule に保存する。

    get_fins_announcement() は引数なし・全銘柄一括取得のため、
    1リクエストで取得した後に保有銘柄コードでフィルタする。

    引数:
        tickers: 保有中の JP ティッカーリスト（通常3件以下）
        today:   バッチ実行日

    返り値: 正常に保存できた銘柄数
    """
    if not _REFRESH_TOKEN or _REFRESH_TOKEN == "YOUR_JQUANTS_API_KEY":
        logger.warning("J-Quants API キーが未設定です。決算予定取得をスキップします。")
        return 0

    client = _get_client()
    today_str = today.strftime("%Y-%m-%d")
    saved = 0

    # 全銘柄の決算予定を1回で取得
    try:
        df_all = client.get_fins_announcement()
    except Exception as e:
        logger.warning(f"決算予定取得失敗（全件）: {e}")
        return 0

    if df_all is None or df_all.empty:
        logger.info("決算予定データなし（全件）")
        return 0

    for ticker in tickers:
        try:
            # universe_master は4桁、J-Quants Code は5桁の場合があるため両方で照合
            jq_code = _to_jquants_code(ticker)
            mask = df_all[_ANN_CODE_COL].astype(str).isin([ticker, jq_code])
            df = df_all[mask]
            if df.empty:
                logger.info(f"  決算予定データなし: {ticker}")
                continue

            rows = []
            for _, r in df.iterrows():
                raw_date = r.get(_ANN_DATE_COL)
                if raw_date is None:
                    continue
                # pandas Timestamp → YYYY-MM-DD 文字列
                ann_date = (
                    raw_date.strftime("%Y-%m-%d")
                    if hasattr(raw_date, "strftime")
                    else str(raw_date).strip()[:10]
                )
                if ann_date < today_str:
                    continue  # 過去分はスキップ
                rows.append({
                    "ticker":            ticker,
                    "announcement_date": ann_date,
                    "fiscal_year":       str(r.get(_ANN_YEAR_COL, "")),
                    "fiscal_quarter":    str(r.get(_ANN_PERIOD_COL, "")),
                    "fetched_at":        today_str,
                })

            if rows:
                with get_connection() as conn:
                    upsert(conn, "earnings_schedule", rows)
                saved += 1
                logger.info(f"  決算予定保存: {ticker} {len(rows)} 件")

        except Exception as e:
            logger.warning(f"決算予定処理失敗: {ticker} ({e})")

    logger.info(f"決算予定更新完了: {saved}/{len(tickers)} 銘柄")
    return saved


# ============================================================
# 内部処理
# ============================================================

def _get_client() -> jquantsapi.Client:
    """J-Quants クライアントを生成する。"""
    return jquantsapi.Client(refresh_token=_REFRESH_TOKEN)


def _fetch_latest_statement(
    client: jquantsapi.Client,
    ticker: str,
    today_str: str,
) -> Optional[dict]:
    """
    指定銘柄の最新財務データを1件取得し、DB 挿入用の辞書を返す。
    取得できない場合は None を返す。
    """
    df = client.get_fins_statements(code=ticker)
    if df is None or df.empty:
        return None

    # 開示日で昇順ソートし最新1件を使用
    if _DATE_COL in df.columns:
        df = df.sort_values(_DATE_COL)
    latest = df.iloc[-1]

    # 財務フィールドの抽出
    row: dict = {
        "ticker":         ticker,
        "fiscal_year":    _extract_fiscal_year(latest.get(_FISCAL_YEAR_END_COL)),
        "fiscal_quarter": str(latest.get(_PERIOD_TYPE_COL, "FY")),
        "fetched_at":     today_str,
    }
    for api_col, db_col in _FIELD_MAP.items():
        row[db_col] = _to_float(latest.get(api_col))

    # PER / PBR / dividend_yield を終値から計算
    close = _get_latest_close(ticker, today_str)
    eps = row.get("eps")
    bps = row.get("bps")
    div = row.get("dividend_per_share")

    row["per"]            = _safe_div(close, eps)
    row["pbr"]            = _safe_div(close, bps)
    row["dividend_yield"] = round(div / close * 100, 2) if (div and close and close > 0) else None

    return row


def _get_latest_close(ticker: str, today_str: str) -> Optional[float]:
    """price_history から指定銘柄の直近終値を取得する。"""
    with get_connection() as conn:
        row = fetch_one(
            conn,
            "SELECT close FROM price_history "
            "WHERE ticker=? AND date<=? AND close IS NOT NULL "
            "ORDER BY date DESC LIMIT 1",
            (ticker, today_str),
        )
    return float(row["close"]) if row else None


def _to_jquants_code(ticker: str) -> str:
    """
    universe_master の4桁数値コード（例: "7203"）を
    J-Quants の5桁コード（例: "72030"）に変換する。
    4桁数字以外はそのまま返す。
    """
    if len(ticker) == 4 and ticker.isdigit():
        return ticker + "0"
    return ticker


def _extract_fiscal_year(val) -> str:
    """
    CurrentFiscalYearEndDate（pandas Timestamp または文字列）から
    会計年度（西暦4桁）を文字列で返す。取得できない場合は空文字。
    """
    try:
        if val is None or val == "":
            return ""
        # pandas Timestamp / datetime の場合
        if hasattr(val, "year"):
            return str(val.year)
        # 文字列 "YYYY-MM-DD" の場合
        return str(str(val)[:4])
    except Exception:
        return ""


def _to_float(val) -> Optional[float]:
    """None・空文字・非数値は None として返す。"""
    try:
        return float(val) if val is not None and val != "" else None
    except (ValueError, TypeError):
        return None


def _safe_div(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    """ゼロ除算・None を安全に処理する除算。小数点2桁で返す。"""
    if numerator is None or denominator is None or denominator == 0:
        return None
    return round(numerator / denominator, 2)
