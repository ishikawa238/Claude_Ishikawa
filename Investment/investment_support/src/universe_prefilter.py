"""
価格取得対象の事前フィルタ

US は universe_master に約 1.8 万銘柄あり、yfinance で全件取得するだけで約 22 分かかる。
一方、直近3か月で日次売買代金が流動性フィルタの下限（screening.us_min_turnover）を
一度でも超えた銘柄は 3,500 件程度しかなく、残りは取得してもスクリーニングで必ず落ちる。

そこで日次バッチでは「直近の売買代金が下限の一定割合を超えた銘柄」だけを取得する。

取りこぼし対策:
- 参照期間内に価格履歴が無い銘柄（新規上場・未取得）は常に対象に含める
- 週1回（既定: 土曜）は全銘柄を取得し、売買代金の実績を更新する
- 保有中の銘柄は常に対象に含める
"""

from datetime import date, timedelta

from src.config import CONFIG
from src.data_store import fetch_all, get_connection
from src.logger import get_logger

logger = get_logger(__name__)

_DEFAULTS = {
    "enabled": True,
    "markets": ["US"],
    "lookback_days": 90,
    # 流動性フィルタ下限に対する倍率。0.25 なら us_min_turnover の 25% 以上で取得対象。
    # 下限ちょうどで切ると、あと一歩で条件を満たす銘柄を永久に拾えなくなるため余裕を持たせる。
    "min_turnover_ratio": 0.25,
    # 全銘柄を取得する曜日（date.weekday(): 月=0 … 土=5）。-1 で無効。
    "full_refresh_weekday": 5,
}


def get_prefilter_config() -> dict:
    """universe_prefilter セクションの設定を既定値とマージして返す。"""
    user = CONFIG.get("universe_prefilter", {}) or {}
    return {**_DEFAULTS, **user}


def is_full_refresh_day(target_date: date, cfg: dict | None = None) -> bool:
    """その日が全銘柄取得日かどうか。"""
    cfg = cfg or get_prefilter_config()
    weekday = int(cfg.get("full_refresh_weekday", -1))
    return weekday >= 0 and target_date.weekday() == weekday


def _min_turnover(market: str, cfg: dict) -> float:
    """取得対象に残す売買代金の下限。"""
    sc = CONFIG["screening"]
    key = "jp_min_turnover" if market.upper() == "JP" else "us_min_turnover"
    return float(sc[key]) * float(cfg.get("min_turnover_ratio", 0.25))


def _recent_max_turnover(tickers: set[str], since: date) -> dict[str, float]:
    """銘柄ごとの参照期間内の最大売買代金（close × volume）を返す。"""
    with get_connection() as conn:
        rows = fetch_all(
            conn,
            "SELECT ticker, MAX(close * volume) AS max_turnover "
            "FROM price_history WHERE date >= ? GROUP BY ticker",
            (since.strftime("%Y-%m-%d"),),
        )
    return {
        r["ticker"]: float(r["max_turnover"] or 0.0)
        for r in rows
        if r["ticker"] in tickers
    }


def _held_tickers() -> set[str]:
    """保有中の銘柄（常に取得対象に含める）。"""
    try:
        with get_connection() as conn:
            rows = fetch_all(
                conn, "SELECT ticker FROM holdings WHERE is_closed = 0",
            )
        return {r["ticker"] for r in rows}
    except Exception as e:  # holdings 未作成などで止めない
        logger.warning(f"保有銘柄の取得に失敗（事前フィルタは継続）: {e}")
        return set()


def select_fetch_tickers(
    tickers_with_market: list[tuple[str, str]],
    market: str,
    target_date: date,
) -> list[tuple[str, str]]:
    """
    価格取得対象を直近の売買代金で絞り込む。

    引数の並び順は維持する。無効化されている場合や全銘柄取得日は入力をそのまま返す。
    """
    cfg = get_prefilter_config()
    markets = [str(m).upper() for m in cfg.get("markets", [])]
    if not cfg.get("enabled", True) or market.upper() not in markets:
        return tickers_with_market
    if is_full_refresh_day(target_date, cfg):
        logger.info(f"事前フィルタ: 全銘柄取得日のため {len(tickers_with_market)} 件すべてを取得します")
        return tickers_with_market

    lookback = int(cfg.get("lookback_days", 90))
    since = target_date - timedelta(days=lookback)
    threshold = _min_turnover(market, cfg)

    all_tickers = {t for t, _ in tickers_with_market}
    turnover = _recent_max_turnover(all_tickers, since)
    always = _held_tickers()

    selected = [
        (t, m) for t, m in tickers_with_market
        # 履歴が無い銘柄（turnover に出てこない）は判断材料が無いので取得する
        if t in always or t not in turnover or turnover[t] >= threshold
    ]
    logger.info(
        f"事前フィルタ({market}): {len(tickers_with_market)} → {len(selected)} 件"
        f"（直近{lookback}日の売買代金 {threshold:,.0f} 以上、または履歴なし・保有中）"
    )
    return selected
