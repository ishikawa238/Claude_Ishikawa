"""
yfinance を使った価格データ取得モジュール

用途:
- fetch_historical.py の初回3年分データ取得
- 日次バッチでのJP株価格取得（EODHD Bulk が JP 未対応のため）
- USD/JPY 為替レート取得（ダッシュボード円換算用）

yfinance の仕様:
- JP株: "7203.T"（東証）
- US株: "AAPL"（そのまま）
- 為替: "USDJPY=X"
- 無料・API キー不要・登録不要
- 非公式 API のため将来的に動作しなくなる可能性がある
"""

import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from src.logger import get_logger

logger = get_logger(__name__)

JP_PRICE_SOURCE = "yfinance"


@dataclass
class DailyPriceFetchResult:
    """日次価格取得の結果サマリー（監視・通知用）。"""

    rows: list[dict]
    total_tickers: int
    tickers_with_target_date: int
    failed_tickers: list[str]
    source: str = JP_PRICE_SOURCE
    status: str = "ok"  # ok / partial / failed

    @property
    def failed_count(self) -> int:
        return len(self.failed_tickers)

    @property
    def alert_message(self) -> str | None:
        """Slack 等に出す警告文。全件失敗時は強調メッセージ。"""
        if self.total_tickers == 0:
            return None
        if self.status == "failed":
            return (
                f"⚠️ *JP価格データ取得失敗*（ソース: {self.source}）\n"
                f"対象 {self.total_tickers} 銘柄のうち当日分を取得できませんでした。"
                " シグナル検出は安全のためスキップされています。"
                " yfinance の障害・レート制限の可能性があります。"
            )
        if self.status == "partial" and self.failed_tickers:
            return (
                f"⚠️ JP価格データ一部失敗（{self.source}）: "
                f"失敗 {len(self.failed_tickers)} / {self.total_tickers} 銘柄"
            )
        return None

_BATCH_SIZE   = 50   # yfinance に一度に渡す銘柄数
_SLEEP_SEC    = 1.0  # バッチ間のウェイト（レートリミット対策）


def _yf_ticker(ticker: str, market: str) -> str:
    """内部ティッカーを yfinance 形式に変換する。"""
    if market == "JP":
        # 数字のみのコードに .T を付与（例: 7203 → 7203.T）
        return f"{ticker}.T"
    return ticker  # US はそのまま


def fetch_history_yf(
    ticker: str,
    market: str,
    from_date: date,
    to_date: date,
) -> list[dict]:
    """
    yfinance で1銘柄の日足履歴を取得する。

    返り値: price_history テーブルへの INSERT 用辞書リスト
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance がインストールされていません: pip install yfinance")
        return []

    yf_tk = _yf_ticker(ticker, market)
    try:
        df = yf.download(
            yf_tk,
            start=from_date.strftime("%Y-%m-%d"),
            end=(to_date + timedelta(days=1)).strftime("%Y-%m-%d"),
            progress=False,
            auto_adjust=True,   # 修正後株価を使用
        )
    except Exception as e:
        logger.warning(f"yfinance 取得失敗: {yf_tk} ({e})")
        return []

    if df is None or df.empty:
        return []

    rows = []
    for idx, row in df.iterrows():
        try:
            close_val = float(row.get("Close", row.get("close", 0)))
            if close_val == 0:
                continue
            rows.append({
                "ticker":     ticker,
                "date":       idx.strftime("%Y-%m-%d"),
                "open":       float(row.get("Open",   row.get("open",   close_val))),
                "high":       float(row.get("High",   row.get("high",   close_val))),
                "low":        float(row.get("Low",    row.get("low",    close_val))),
                "close":      close_val,
                "adj_close":  close_val,   # auto_adjust=True なので close = adj_close
                "volume":     float(row.get("Volume", row.get("volume", 0))) or None,
                "is_holiday": 0,
            })
        except (ValueError, TypeError):
            continue

    return rows


def fetch_history_bulk_yf(
    tickers_with_market: list[tuple[str, str]],
    from_date: date,
    to_date: date,
) -> tuple[list[dict], list[str]]:
    """
    yfinance で複数銘柄の日足履歴を一括取得する。
    _BATCH_SIZE 件ごとにまとめてダウンロードする（yfinance のマルチティッカー機能）。

    返り値: (成功した全行データのリスト, 失敗したティッカーのリスト)
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance がインストールされていません: pip install yfinance")
        return [], [t for t, _ in tickers_with_market]

    all_rows: list[dict] = []
    failed: list[str] = []
    total = len(tickers_with_market)

    # _BATCH_SIZE 件ずつ処理
    for batch_start in range(0, total, _BATCH_SIZE):
        batch = tickers_with_market[batch_start:batch_start + _BATCH_SIZE]
        yf_tickers = [_yf_ticker(t, m) for t, m in batch]
        internal_map = {_yf_ticker(t, m): t for t, m in batch}
        market_map   = {_yf_ticker(t, m): m for t, m in batch}

        logger.info(f"  yfinance 取得: {batch_start + 1}〜{batch_start + len(batch)} / {total}")

        try:
            df = yf.download(
                yf_tickers,
                start=from_date.strftime("%Y-%m-%d"),
                end=(to_date + timedelta(days=1)).strftime("%Y-%m-%d"),
                progress=False,
                auto_adjust=True,
                group_by="ticker",
            )
        except Exception as e:
            logger.warning(f"  バッチ取得失敗（{batch_start}〜）: {e}")
            failed.extend(t for t, _ in batch)
            time.sleep(_SLEEP_SEC)
            continue

        if df is None or df.empty:
            failed.extend(t for t, _ in batch)
            time.sleep(_SLEEP_SEC)
            continue

        # マルチティッカーの場合は2階層のカラム、シングルの場合は1階層
        for yf_tk in yf_tickers:
            internal_tk = internal_map.get(yf_tk, yf_tk)
            try:
                if len(yf_tickers) == 1:
                    tk_df = df
                else:
                    if yf_tk not in df.columns.get_level_values(0):
                        failed.append(internal_tk)
                        continue
                    tk_df = df[yf_tk]

                if tk_df.empty:
                    failed.append(internal_tk)
                    continue

                for idx, row in tk_df.iterrows():
                    close_val = None
                    for col in ["Close", "close"]:
                        if col in row and row[col] and float(row[col]) > 0:
                            close_val = float(row[col])
                            break
                    if close_val is None:
                        continue

                    all_rows.append({
                        "ticker":    internal_tk,
                        "date":      idx.strftime("%Y-%m-%d"),
                        "open":      float(row.get("Open",   row.get("open",   close_val))),
                        "high":      float(row.get("High",   row.get("high",   close_val))),
                        "low":       float(row.get("Low",    row.get("low",    close_val))),
                        "close":     close_val,
                        "adj_close": close_val,
                        "volume":    float(row.get("Volume", row.get("volume", 0))) or None,
                        "is_holiday": 0,
                    })
            except Exception as e:
                logger.warning(f"  {yf_tk} パース失敗: {e}")
                failed.append(internal_tk)

        time.sleep(_SLEEP_SEC)

    logger.info(
        f"yfinance 一括取得完了: 成功 {total - len(failed)} 件 / "
        f"失敗 {len(failed)} 件 / 総行数 {len(all_rows)}"
    )
    return all_rows, failed


def fetch_daily_yf_with_status(
    tickers_with_market: list[tuple[str, str]],
    target_date: date,
) -> DailyPriceFetchResult:
    """
    yfinance で指定日の価格データを一括取得し、成否サマリーを返す。
    fetch_bulk_eod("JP") の代替（JP 市場の EODHD Bulk が利用不可の場合に使用）。
    """
    total = len(tickers_with_market)
    if total == 0:
        return DailyPriceFetchResult(
            rows=[], total_tickers=0, tickers_with_target_date=0,
            failed_tickers=[], status="ok",
        )

    from_date = target_date - timedelta(days=5)
    all_rows, failed = fetch_history_bulk_yf(tickers_with_market, from_date, target_date)

    target_str = target_date.strftime("%Y-%m-%d")
    daily_rows = [r for r in all_rows if r["date"] == target_str]
    tickers_on_date = {r["ticker"] for r in daily_rows}

    min_coverage = total * 0.5
    if daily_rows and len(daily_rows) >= min_coverage:
        rows = daily_rows
        logger.info(f"yfinance 当日価格取得完了: {len(daily_rows)} 件 ({target_str})")
    else:
        rows = all_rows
        logger.info(
            f"yfinance 当日価格なし ({target_str})。直近データ {len(all_rows)} 件を返します"
        )

    if not rows:
        status = "failed"
    elif failed or len(tickers_on_date) < min_coverage:
        status = "partial" if rows else "failed"
    else:
        status = "ok"

    return DailyPriceFetchResult(
        rows=rows,
        total_tickers=total,
        tickers_with_target_date=len(tickers_on_date),
        failed_tickers=failed,
        source=JP_PRICE_SOURCE,
        status=status,
    )


def fetch_daily_yf(
    tickers_with_market: list[tuple[str, str]],
    target_date: date,
) -> list[dict]:
    """
    yfinance で指定日の価格データを一括取得する（後方互換ラッパー）。

    返り値: price_history テーブルへの INSERT 用辞書リスト。
    """
    return fetch_daily_yf_with_status(tickers_with_market, target_date).rows


def fetch_usdjpy_rate() -> float | None:
    """
    yfinance で最新の USD/JPY 為替レートを取得する。

    ダッシュボードの資産総額円換算に使用する。
    取得できない場合は None を返す（呼び出し側でフォールバック値を使用する）。
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance がインストールされていません: pip install yfinance")
        return None

    try:
        ticker = yf.Ticker("USDJPY=X")
        info = ticker.fast_info
        rate = float(getattr(info, "last_price", 0) or getattr(info, "previous_close", 0))
        if rate > 0:
            logger.info(f"USD/JPY レート取得: {rate:.2f}")
            return rate
    except Exception as e:
        logger.warning(f"USD/JPY レート取得失敗: {e}")

    return None
