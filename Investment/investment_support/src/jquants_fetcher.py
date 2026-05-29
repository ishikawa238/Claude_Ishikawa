"""
J-Quants データ取得モジュール

役割:
- J-Quants API（fins/statements）から財務データを取得
- fundamentals_snapshot テーブルへの UPSERT
- universe_master.shares_outstanding の更新

対象市場: JP のみ（J-Quants は国内株専用）

API コール設計:
- V2 月次: bulk API（/fins/summary）で一括取得（推奨・数リクエスト）
- フォールバック: 銘柄ごとに get_fin_summary（request_interval_sec でレート制限回避）

注意:
- config.json の jquants.api_key に **V2 API キー**（ダッシュボード発行）を設定すること（推奨）
- 旧 V1 利用者のみ jquants.auth_type を "refresh_token" にし、リフレッシュトークンを api_key に設定
- PER / PBR / dividend_yield は price_history の最新終値から計算（API から直接取得しない）
"""

import re
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Optional

import jquantsapi
import pandas as pd
from jquantsapi.enums import BulkEndpoint

from src.config import CONFIG
from src.data_store import fetch_all, fetch_one, get_connection, upsert
from src.logger import get_logger

logger = get_logger(__name__)

# ============================================================
# 定数
# ============================================================

_JQ = CONFIG.get("jquants", {})
_API_KEY = (_JQ.get("api_key") or "").strip()
_AUTH_TYPE = str(_JQ.get("auth_type", "api_key")).strip().lower()
# 無料プランは 5 req/min → 既定 13 秒間隔（プランに応じて config で短縮可）
_INTERVAL = float(_JQ.get("request_interval_sec", 13.0))
_USE_BULK_FUNDAMENTALS = bool(_JQ.get("use_bulk_fundamentals", True))
_RATE_LIMIT_WAIT_SEC = float(_JQ.get("rate_limit_wait_sec", 65.0))
_MAX_RETRIES = int(_JQ.get("max_retries", 3))
# 0=無制限。無料プランは 5 req/min のため 300 件/回程度を推奨（約65分）
_MAX_FUNDAMENTALS_PER_RUN = int(_JQ.get("max_fundamentals_per_run", 300))

# V1 fins/statements フィールド名 → 内部カラム名
_FIELD_MAP_V1 = {
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

# V2 fins/summary フィールド名 → 内部カラム名
_FIELD_MAP_V2 = {
    "Sales":    "revenue",
    "OP":       "operating_income",
    "OdP":      "ordinary_income",
    "NP":       "net_income",
    "EPS":      "eps",
    "BPS":      "bps",
    "ShOutFY":  "shares_outstanding",
    "DivAnn":   "dividend_per_share",
}

# V1 会計期間・開示日カラム
_V1_PERIOD_TYPE_COL     = "TypeOfCurrentPeriod"
_V1_FISCAL_YEAR_END_COL = "CurrentFiscalYearEndDate"
_V1_DATE_COL            = "DisclosedDate"

# V2 会計期間・開示日カラム
_V2_PERIOD_TYPE_COL     = "CurPerType"
_V2_FISCAL_YEAR_END_COL = "CurFYEn"
_V2_DATE_COL            = "DiscDate"

# 決算予定（announcement / earnings-calendar）
_ANN_DATE_COL    = "Date"
_ANN_PERIOD_COL_V1 = "FiscalQuarter"
_ANN_YEAR_COL_V1   = "FiscalYear"
_ANN_PERIOD_COL_V2 = "FQ"
_ANN_YEAR_COL_V2   = "FY"
_ANN_CODE_COL    = "Code"


class JQuantsAuthError(Exception):
    """J-Quants 認証・API キー設定エラー"""


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
    if not _is_configured():
        logger.warning("J-Quants API キーが未設定です。財務データ取得をスキップします。")
        return 0

    try:
        client, api_version = _create_client()
    except JQuantsAuthError as e:
        logger.error("%s", e)
        return 0

    today_str = today.strftime("%Y-%m-%d")
    work_tickers = _prioritize_fundamentals_tickers(tickers)
    if len(work_tickers) < len(tickers):
        logger.info(
            "財務データ取得: プラン制限のため %d/%d 銘柄に限定（max_fundamentals_per_run）",
            len(work_tickers),
            len(tickers),
        )
    ticker_set = set(work_tickers)

    if api_version == "v2" and _USE_BULK_FUNDAMENTALS:
        bulk_saved = _fetch_and_save_fundamentals_bulk_v2(client, ticker_set, today_str)
        if bulk_saved is not None:
            logger.info(
                f"財務データ更新完了（bulk）: {bulk_saved}/{len(work_tickers)} 銘柄"
            )
            return bulk_saved
        logger.info("bulk 取得に失敗したため、銘柄別取得にフォールバックします。")

    saved = _fetch_and_save_fundamentals_per_ticker(
        client, api_version, work_tickers, today_str,
    )
    logger.info(f"財務データ更新完了: {saved}/{len(work_tickers)} 銘柄")
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
    if not _is_configured():
        logger.warning("J-Quants API キーが未設定です。決算予定取得をスキップします。")
        return 0

    try:
        client, api_version = _create_client()
    except JQuantsAuthError as e:
        logger.error("%s", e)
        return 0

    today_str = today.strftime("%Y-%m-%d")
    saved = 0
    ann_year_col = _ANN_YEAR_COL_V2 if api_version == "v2" else _ANN_YEAR_COL_V1
    ann_period_col = _ANN_PERIOD_COL_V2 if api_version == "v2" else _ANN_PERIOD_COL_V1

    # 全銘柄の決算予定を1回で取得
    try:
        if api_version == "v2":
            df_all = client.get_eq_earnings_cal()
        else:
            df_all = client.get_fins_announcement()
    except JQuantsAuthError as e:
        logger.error("決算予定取得失敗（認証）: %s", e)
        return 0
    except Exception as e:
        if _is_auth_failure(e):
            logger.error(
                "決算予定取得失敗（認証）。V2 API キーと auth_type=api_key を確認してください: %s",
                e,
            )
            return 0
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
                    "fiscal_year":       str(r.get(ann_year_col, "")),
                    "fiscal_quarter":    str(r.get(ann_period_col, "")),
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

def _fetch_and_save_fundamentals_bulk_v2(
    client,
    ticker_set: set[str],
    today_str: str,
) -> Optional[int]:
    """
    V2 bulk API で財務サマリを一括取得して DB に保存する。
    失敗時は None を返し、呼び出し元が銘柄別取得にフォールバックする。
    """
    try:
        listing = client.get_bulk_list(BulkEndpoint.FIN_SUMMARY)
        if listing is None or listing.empty:
            logger.warning("J-Quants bulk /fins/summary 一覧が空です。")
            return None

        listing = listing.sort_values("LastModified", ascending=False)
        bulk_key = str(listing.iloc[0]["Key"])
        logger.info(
            "J-Quants bulk 財務データをダウンロードします: key=%s (%s)",
            bulk_key,
            listing.iloc[0].get("LastModified", ""),
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_path = Path(tmp_dir) / "fin_summary.csv.gz"
            client.download_bulk(bulk_key, str(out_path))
            df = pd.read_csv(out_path)

        if df is None or df.empty:
            logger.warning("J-Quants bulk 財務データが空です。")
            return None

        if _V2_DATE_COL in df.columns:
            df[_V2_DATE_COL] = pd.to_datetime(df[_V2_DATE_COL], errors="coerce")

        df["_ticker"] = df["Code"].astype(str).map(_bulk_code_to_ticker)
        df = df[df["_ticker"].isin(ticker_set)]
        if df.empty:
            logger.warning("bulk 財務データに対象銘柄が含まれませんでした。")
            return None

        df = df.sort_values([ "_ticker", _V2_DATE_COL])
        latest_by_ticker = df.groupby("_ticker", as_index=False).tail(1)

        saved = 0
        batch: list[dict] = []
        for _, series in latest_by_ticker.iterrows():
            ticker = str(series["_ticker"])
            row = _row_from_fin_series(
                series, ticker, today_str,
                field_map=_FIELD_MAP_V2,
                period_col=_V2_PERIOD_TYPE_COL,
                fiscal_end_col=_V2_FISCAL_YEAR_END_COL,
            )
            if row is None:
                continue
            batch.append(row)
            if len(batch) >= 500:
                saved += _persist_fundamental_rows(batch, today_str)
                batch = []
        if batch:
            saved += _persist_fundamental_rows(batch, today_str)

        return saved

    except Exception as e:
        if _is_plan_restriction(e):
            logger.info(
                "J-Quants bulk API は現在のプランでは利用できません（銘柄別取得に切り替え）。"
            )
            return None
        if _is_auth_failure(e):
            logger.error("J-Quants bulk 取得失敗（認証）: %s", e)
            return None
        logger.warning("J-Quants bulk 取得失敗: %s", e)
        return None


def _fetch_and_save_fundamentals_per_ticker(
    client,
    api_version: str,
    tickers: list[str],
    today_str: str,
) -> int:
    """銘柄ごとに財務サマリを取得（レート制限時は待機してリトライ）。"""
    saved = 0
    total = len(tickers)

    for i, ticker in enumerate(tickers, start=1):
        if i % 100 == 0:
            logger.info(f"  財務データ取得進捗: {i}/{total}")

        try:
            time.sleep(_INTERVAL)
            row = _fetch_latest_statement_with_retry(
                client, api_version, ticker, today_str,
            )
            if row is None:
                continue
            saved += _persist_fundamental_rows([row], today_str)

        except JQuantsAuthError as e:
            logger.error("J-Quants 認証エラーのため財務データ取得を中断します: %s", e)
            break
        except Exception as e:
            if _is_auth_failure(e):
                logger.error(
                    "J-Quants 認証に失敗しました（%s）。API キーと auth_type を確認してください: %s",
                    api_version,
                    e,
                )
                break
            logger.warning(f"財務データ取得失敗: {ticker} ({e})")

    return saved


def _persist_fundamental_rows(rows: list[dict], today_str: str) -> int:
    """fundamentals_snapshot と shares_outstanding を保存する。"""
    if not rows:
        return 0
    with get_connection() as conn:
        upsert(conn, "fundamentals_snapshot", rows)
        for row in rows:
            shares = row.get("shares_outstanding")
            if shares is not None:
                conn.execute(
                    "UPDATE universe_master SET shares_outstanding=?, last_updated=? "
                    "WHERE ticker=?",
                    (shares, today_str, row["ticker"]),
                )
    return len(rows)


def _fetch_latest_statement_with_retry(
    client,
    api_version: str,
    ticker: str,
    today_str: str,
) -> Optional[dict]:
    for attempt in range(_MAX_RETRIES):
        try:
            return _fetch_latest_statement(client, api_version, ticker, today_str)
        except Exception as e:
            if _is_rate_limit(e) and attempt < _MAX_RETRIES - 1:
                logger.warning(
                    "J-Quants レート制限 (429)。%.0f 秒待機してリトライ (%s)…",
                    _RATE_LIMIT_WAIT_SEC,
                    ticker,
                )
                time.sleep(_RATE_LIMIT_WAIT_SEC)
                continue
            raise
    return None


def _is_configured() -> bool:
    return bool(_API_KEY) and _API_KEY != "YOUR_JQUANTS_API_KEY"


def _create_client():
    """
    J-Quants クライアントを生成する。

    返り値: (client, api_version)  api_version は "v2" または "v1"
    """
    if _AUTH_TYPE in ("refresh_token", "v1", "refresh"):
        return jquantsapi.Client(refresh_token=_API_KEY), "v1"
    if _AUTH_TYPE in ("api_key", "v2", "apikey", ""):
        try:
            return jquantsapi.ClientV2(api_key=_API_KEY), "v2"
        except ValueError as e:
            raise JQuantsAuthError(
                "J-Quants V2 API キーが無効です。"
                " https://jpx-jquants.com/dashboard/api-keys で API キーを発行し、"
                ' config.json の jquants.api_key に設定してください。'
            ) from e
    raise JQuantsAuthError(
        f'jquants.auth_type が不正です: "{_AUTH_TYPE}"。'
        '"api_key"（推奨）または "refresh_token" を指定してください。'
    )


def _prioritize_fundamentals_tickers(tickers: list[str]) -> list[str]:
    """無料プラン向け: 株式数未登録銘柄を優先し、件数上限を適用する。"""
    if _MAX_FUNDAMENTALS_PER_RUN <= 0 or len(tickers) <= _MAX_FUNDAMENTALS_PER_RUN:
        return tickers
    with get_connection() as conn:
        missing = fetch_all(
            conn,
            "SELECT ticker FROM universe_master "
            "WHERE market='JP' AND is_active=1 AND is_excluded=0 "
            "AND shares_outstanding IS NULL",
        )
    missing_set = {r["ticker"] for r in missing}
    prioritized = [t for t in tickers if t in missing_set]
    rest = [t for t in tickers if t not in missing_set]
    return (prioritized + rest)[:_MAX_FUNDAMENTALS_PER_RUN]


def _is_rate_limit(exc: Exception) -> bool:
    return "429" in str(exc)


def _is_plan_restriction(exc: Exception) -> bool:
    """サブスクリプションで API / 機能が使えない場合（認証成功だが 403）。"""
    msg = str(exc).lower()
    return (
        "not available on your subscription" in msg
        or "not available on your plan" in msg
        or ("subscription" in msg and "403" in msg)
    )


def _is_auth_failure(exc: Exception) -> bool:
    """V1 リフレッシュ失敗・V2 認証失敗を判定する（429・プラン制限・銘柄コードは除外）。"""
    from jquantsapi.client import TokenAuthRefreshBadRequestException

    if _is_rate_limit(exc) or _is_plan_restriction(exc):
        return False
    if isinstance(exc, TokenAuthRefreshBadRequestException):
        return True
    msg = str(exc).lower()
    if "auth_refresh" in msg or "unauthorized" in msg:
        return True
    if "bad request for url" in msg and "token" in msg:
        return True
    # URL 内の銘柄コード (例: code=14010) に "401" が含まれる誤判定を防ぐ
    return bool(re.search(r"\b401\b", msg) or re.search(r"\b403\b", msg))


def _fetch_latest_statement(
    client,
    api_version: str,
    ticker: str,
    today_str: str,
) -> Optional[dict]:
    """
    指定銘柄の最新財務データを1件取得し、DB 挿入用の辞書を返す。
    取得できない場合は None を返す。
    """
    jq_code = _to_jquants_code(ticker)
    if api_version == "v2":
        df = client.get_fin_summary(code=jq_code)
        field_map = _FIELD_MAP_V2
        period_col = _V2_PERIOD_TYPE_COL
        fiscal_end_col = _V2_FISCAL_YEAR_END_COL
        date_col = _V2_DATE_COL
    else:
        df = client.get_fins_statements(code=ticker)
        field_map = _FIELD_MAP_V1
        period_col = _V1_PERIOD_TYPE_COL
        fiscal_end_col = _V1_FISCAL_YEAR_END_COL
        date_col = _V1_DATE_COL

    if df is None or df.empty:
        return None

    if date_col in df.columns:
        df = df.sort_values(date_col)
    return _row_from_fin_series(
        df.iloc[-1], ticker, today_str,
        field_map=field_map,
        period_col=period_col,
        fiscal_end_col=fiscal_end_col,
    )


def _row_from_fin_series(
    latest,
    ticker: str,
    today_str: str,
    *,
    field_map: dict,
    period_col: str,
    fiscal_end_col: str,
) -> Optional[dict]:
    row: dict = {
        "ticker":         ticker,
        "fiscal_year":    _extract_fiscal_year(latest.get(fiscal_end_col)),
        "fiscal_quarter": str(latest.get(period_col, "FY")),
        "fetched_at":     today_str,
    }
    for api_col, db_col in field_map.items():
        row[db_col] = _to_float(latest.get(api_col))

    close = _get_latest_close(ticker, today_str)
    eps = row.get("eps")
    bps = row.get("bps")
    div = row.get("dividend_per_share")

    row["per"]            = _safe_div(close, eps)
    row["pbr"]            = _safe_div(close, bps)
    row["dividend_yield"] = (
        round(div / close * 100, 2) if (div and close and close > 0) else None
    )
    return row


def _bulk_code_to_ticker(code: str) -> str:
    """bulk の 5 桁 Code (72030) を universe の 4 桁 ticker (7203) に変換。"""
    s = str(code).strip()
    if len(s) == 5 and s.isdigit() and s.endswith("0"):
        return s[:4]
    return s


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
