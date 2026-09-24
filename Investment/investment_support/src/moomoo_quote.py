"""
moomoo OpenD から日足＋スナップショットを取得し、価格系列を構築する。
"""

from __future__ import annotations

import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Iterator

from shared.net import is_port_open
from src.logger import get_logger
from src.technical_calculator import calculate_snapshot

logger = get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_MOOMOO_SDK = _PROJECT_ROOT / "MMAPI4Python_10.2.6208"
_MARKET_PREFIX = {"JP": "JP", "US": "US"}
_KLINE_LOOKBACK_DAYS = 400
_KLINE_MAX_COUNT = 300
_SNAPSHOT_BATCH_SIZE = 200
_KLINE_PAUSE_SEC = 0.15


def ensure_sdk_path() -> None:
    sdk = str(_MOOMOO_SDK)
    if sdk not in sys.path:
        sys.path.insert(0, sdk)


def parse_moomoo_code(code: str) -> tuple[str, str] | None:
    """moomoo code → (ticker, market)。JP/US のみ。"""
    code = (code or "").strip()
    if "." not in code:
        return None
    prefix, ticker = code.split(".", 1)
    market = _MARKET_PREFIX.get(prefix.upper())
    if not market:
        return None
    return ticker.strip().upper(), market


def merge_snapshot_bar(
    prices: list[dict],
    snapshot_row: dict | None,
    *,
    today: date | None = None,
) -> list[dict]:
    """日足系列に当日スナップショットを合成する。"""
    if not snapshot_row:
        return prices
    target_day = (today or date.today()).strftime("%Y-%m-%d")
    last_price = snapshot_row.get("last_price")
    if last_price is None:
        return prices
    bar = {
        "date": target_day,
        "open": float(snapshot_row.get("open_price") or last_price),
        "high": float(snapshot_row.get("high_price") or last_price),
        "low": float(snapshot_row.get("low_price") or last_price),
        "close": float(last_price),
        "adj_close": float(last_price),
        "volume": float(snapshot_row.get("volume") or 0),
    }
    out = list(prices)
    if out and out[-1]["date"] == target_day:
        out[-1] = bar
    else:
        out.append(bar)
    return out


def kline_to_prices(kline_df, snapshot_row: dict | None, *, today: date | None = None) -> list[dict]:
    """日足 DataFrame とスナップショットから price_history 互換の系列を作る。"""
    prices: list[dict] = []
    for _, row in kline_df.iterrows():
        time_key = str(row.get("time_key", ""))[:10]
        if not time_key:
            continue
        close = float(row.get("close") or 0)
        prices.append({
            "date": time_key,
            "open": float(row.get("open") or close),
            "high": float(row.get("high") or close),
            "low": float(row.get("low") or close),
            "close": close,
            "adj_close": close,
            "volume": float(row.get("volume") or 0),
        })
    if not snapshot_row:
        return prices
    target_day = (today or date.today()).strftime("%Y-%m-%d")
    last_price = snapshot_row.get("last_price")
    if last_price is None:
        return prices
    bar = {
        "date": target_day,
        "open": float(snapshot_row.get("open_price") or last_price),
        "high": float(snapshot_row.get("high_price") or last_price),
        "low": float(snapshot_row.get("low_price") or last_price),
        "close": float(last_price),
        "adj_close": float(last_price),
        "volume": float(snapshot_row.get("volume") or 0),
    }
    if prices and prices[-1]["date"] == target_day:
        prices[-1] = bar
    else:
        prices.append(bar)
    return prices


def rsi_history_from_prices(prices: list[dict], lookback: int) -> list[float]:
    """直近 lookback 日分の RSI を日付昇順で返す（当日含む）。"""
    if lookback <= 0 or not prices:
        return []
    out: list[float] = []
    for i in range(len(prices)):
        sub = prices[: i + 1]
        snap = calculate_snapshot("__rsi__", date.fromisoformat(sub[-1]["date"]), sub)
        rsi = snap.get("rsi14")
        if rsi is not None:
            out.append(float(rsi))
    return out[-lookback:]


class MoomooQuoteClient:
    """OpenQuoteContext を使い回して kline / snapshot を取得する。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 11111) -> None:
        # SDK は OpenD 未起動だと接続を無期限に再試行して戻らないため、先に確認する
        if not is_port_open(host, port):
            raise ConnectionError(f"moomoo OpenD に接続できません ({host}:{port})")
        ensure_sdk_path()
        from moomoo import OpenQuoteContext

        self.host = host
        self.port = port
        self._ctx = OpenQuoteContext(host=host, port=port)

    def close(self) -> None:
        try:
            self._ctx.close()
        except Exception:
            pass

    def __enter__(self) -> MoomooQuoteClient:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def fetch_snapshots(self, codes: list[str]) -> dict[str, dict]:
        """code → snapshot row dict。"""
        from moomoo.common.constant import RET_OK

        result: dict[str, dict] = {}
        for i in range(0, len(codes), _SNAPSHOT_BATCH_SIZE):
            chunk = codes[i: i + _SNAPSHOT_BATCH_SIZE]
            ret, data = self._ctx.get_market_snapshot(chunk)
            if ret != RET_OK:
                logger.warning(f"moomoo snapshot 失敗: {data}")
                continue
            if data is None or data.empty:
                continue
            for _, row in data.iterrows():
                code = str(row.get("code", ""))
                result[code] = row.to_dict()
        return result

    def fetch_kline(self, code: str, *, today: date | None = None) -> tuple[bool, list[dict], str | None]:
        """(成功, prices, エラーメッセージ)"""
        from moomoo.common.constant import RET_OK, AuType, KLType

        start = ((today or date.today()) - timedelta(days=_KLINE_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        ret, kline, _ = self._ctx.request_history_kline(
            code,
            start=start,
            end=None,
            ktype=KLType.K_DAY,
            autype=AuType.QFQ,
            max_count=_KLINE_MAX_COUNT,
        )
        if ret != RET_OK:
            return False, [], str(kline)
        return True, [
            {
                "date": str(row.get("time_key", ""))[:10],
                "open": float(row.get("open") or 0),
                "high": float(row.get("high") or 0),
                "low": float(row.get("low") or 0),
                "close": float(row.get("close") or 0),
                "adj_close": float(row.get("close") or 0),
                "volume": float(row.get("volume") or 0),
            }
            for _, row in kline.iterrows()
            if str(row.get("time_key", ""))[:10]
        ], None

    def build_technical_row(
        self,
        code: str,
        name: str,
        snapshot_row: dict | None,
        *,
        today: date | None = None,
    ) -> dict | None:
        """kline + snapshot から swing 評価用の tk dict を構築。"""
        parsed = parse_moomoo_code(code)
        if not parsed:
            return None
        ticker, market = parsed
        ok, raw_prices, err = self.fetch_kline(code, today=today)
        if not ok:
            logger.warning(f"kline 取得失敗 {code}: {err}")
            return None
        prices = merge_snapshot_bar(raw_prices, snapshot_row, today=today)
        if not prices:
            return None
        target = today or date.today()
        snap = calculate_snapshot(ticker, target, prices)
        vol = snap.get("volume")
        vol_avg = snap.get("vol_20d_avg")
        vol_ratio = (vol / vol_avg) if (vol and vol_avg and vol_avg > 0) else None
        return {
            "ticker": ticker,
            "market": market,
            "moomoo_code": code,
            "name": name or snapshot_row.get("name") if snapshot_row else name,
            "close": snap.get("close") or (snapshot_row or {}).get("last_price"),
            "ema25": snap.get("ema25"),
            "ema75": snap.get("ema75"),
            "rsi14": snap.get("rsi14"),
            "atr14": snap.get("atr14"),
            "volume": vol,
            "vol_20d_avg": vol_avg,
            "vol_ratio": vol_ratio,
            "macd_diff": snap.get("macd_diff"),
            "macd_dea": snap.get("macd_dea"),
            "macd_hist": snap.get("macd_hist"),
            "macd_diff_prev": snap.get("macd_diff_prev"),
            "macd_dea_prev": snap.get("macd_dea_prev"),
            "macd_hist_prev": snap.get("macd_hist_prev"),
            "_prices": prices,
        }


def iter_technical_rows(
    securities: list[dict],
    host: str,
    port: int,
    *,
    today: date | None = None,
) -> Iterator[dict]:
    """お気に入り銘柄リストからテクニカル行を逐次 yield。"""
    if not securities:
        return
    codes = [s["moomoo_code"] for s in securities if s.get("moomoo_code")]
    with MoomooQuoteClient(host, port) as client:
        snapshots = client.fetch_snapshots(codes)
        for sec in securities:
            code = sec.get("moomoo_code")
            if not code:
                continue
            row = client.build_technical_row(
                code,
                sec.get("name") or "",
                snapshots.get(code),
                today=today,
            )
            if row:
                yield row
            time.sleep(_KLINE_PAUSE_SEC)
