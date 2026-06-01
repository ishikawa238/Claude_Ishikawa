"""
Screening List → Prompt Builder 連携

daily_signals の最新行から、プロンプト用の候補 dict を組み立てる。
trade_signal_log に無い watch 銘柄も購入相談プロンプトに載せられる。
"""

from __future__ import annotations

from src.data_store import fetch_one
from src.entry_calculator import calc_entry_set
from src.logger import get_logger

logger = get_logger(__name__)

_DAILY_SIGNAL_ROW_SQL = """
    SELECT d.ticker, d.date, d.market, d.pattern, d.label,
           d.close, d.rsi14, d.atr14, d.vol_ratio,
           d.final_score, d.recommend_rank, d.recommend_reason,
           d.market_mode,
           d.trend_score, d.pattern_a_score, d.pattern_b_score,
           d.pattern_c_score, d.liquidity_score,
           u.name,
           t.rsi14 AS tech_rsi14, t.ma5 AS tech_ma5, t.ma25 AS tech_ma25,
           t.ma75 AS tech_ma75, t.ma200 AS tech_ma200,
           t.vol_20d_avg, t.high_52w AS tech_high_52w,
           ph.adj_close AS ph_close,
           ph.volume AS ph_volume
    FROM daily_signals d
    LEFT JOIN universe_master u ON u.ticker = d.ticker
    LEFT JOIN technical_snapshot t
      ON t.ticker = d.ticker
      AND t.date = (SELECT MAX(date) FROM technical_snapshot WHERE ticker = d.ticker)
    LEFT JOIN price_history ph
      ON ph.ticker = d.ticker
      AND ph.date = (
          SELECT MAX(date) FROM price_history
          WHERE ticker = d.ticker AND is_holiday = 0
      )
    WHERE d.ticker = ?
      AND d.date = (SELECT MAX(date) FROM daily_signals WHERE market = d.market)
"""


def enrich_tickers_from_daily_signals(
    conn,
    tickers: list[str],
    *,
    open_holdings_count: int = 0,
) -> list[dict]:
    """Screening 由来のティッカーを Prompt Builder 用候補 dict に変換する。"""
    if not tickers:
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for raw in tickers:
        ticker = (raw or "").strip()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)

        row = fetch_one(conn, _DAILY_SIGNAL_ROW_SQL, (ticker,))
        if not row:
            logger.info(f"Screening→Prompt: daily_signals に無いためスキップ: {ticker}")
            continue

        cand = _row_to_prompt_candidate(dict(row), open_holdings_count)
        if cand:
            out.append(cand)
    return out


def _row_to_prompt_candidate(row: dict, open_holdings_count: int) -> dict:
    """daily_signals 1行 → trade_signal_log 互換の enriched dict。"""
    close = row.get("close") or row.get("ph_close")
    signal = {
        "date": row.get("date"),
        "ticker": row["ticker"],
        "name": row.get("name"),
        "market": row["market"],
        "pattern": row.get("pattern") or "A",
        "close": close,
        "atr14": row.get("atr14"),
        "market_mode": row.get("market_mode") or "normal",
    }
    entry = calc_entry_set(signal, open_holdings_count=open_holdings_count)

    vol_ratio = row.get("vol_ratio")
    if vol_ratio is None and row.get("vol_20d_avg") and row.get("ph_volume"):
        vol_avg = row["vol_20d_avg"]
        if vol_avg > 0:
            vol_ratio = row["ph_volume"] / vol_avg

    cand: dict = {
        "ticker": row["ticker"],
        "name": row.get("name"),
        "market": row["market"],
        "pattern": row.get("pattern"),
        "signal_date": row.get("date"),
        "final_score": row.get("final_score"),
        "recommend_rank": row.get("recommend_rank"),
        "ds_label": row.get("label"),
        "recommend_reason": row.get("recommend_reason"),
        "_trend_score": row.get("trend_score"),
        "_pattern_a_score": row.get("pattern_a_score"),
        "_pattern_b_score": row.get("pattern_b_score"),
        "_pattern_c_score": row.get("pattern_c_score"),
        "_liquidity_score": row.get("liquidity_score"),
        "_rsi14": row.get("rsi14") or row.get("tech_rsi14"),
        "_ma5": row.get("tech_ma5"),
        "_ma25": row.get("tech_ma25"),
        "_ma75": row.get("tech_ma75"),
        "_ma200": row.get("tech_ma200"),
        "_vol_ratio": vol_ratio,
        "_high_52w": row.get("tech_high_52w"),
        "_close": close,
        "_from_screening": True,
    }

    if entry:
        cand.update({
            "entry_price": entry.get("entry_price"),
            "stop_loss_price": entry.get("stop_loss_price"),
            "stop_loss_pct": entry.get("stop_loss_pct"),
            "target_price": entry.get("target_price"),
            "recommended_amount": entry.get("recommended_amount"),
            "conditions_met": entry.get("conditions_met"),
        })
    elif close:
        cand.update({
            "entry_price": round(float(close), 2),
            "stop_loss_price": None,
            "stop_loss_pct": None,
            "target_price": None,
            "recommended_amount": None,
            "_skip_reason": "エントリーセット未算出（損切り幅不足等）",
        })

    return cand


def merge_prompt_candidates(
    signal_log_candidates: list[dict],
    screening_candidates: list[dict],
    *,
    prefer_screening: bool = False,
) -> list[dict]:
    """候補リストをティッカー単位でマージする。"""
    by_ticker: dict[str, dict] = {c["ticker"]: c for c in signal_log_candidates if c.get("ticker")}

    for sc in screening_candidates:
        t = sc.get("ticker")
        if not t:
            continue
        if prefer_screening or t not in by_ticker:
            by_ticker[t] = sc
        else:
            merged = dict(by_ticker[t])
            merged["_from_screening"] = True
            for key, val in sc.items():
                if val is not None and key.startswith("_"):
                    merged[key] = val
            by_ticker[t] = merged

    return sorted(
        by_ticker.values(),
        key=lambda r: (r.get("final_score") or 0),
        reverse=True,
    )


def count_open_holdings(conn) -> int:
    row = fetch_one(
        conn,
        "SELECT COUNT(*) AS cnt FROM holdings WHERE is_closed = 0",
    )
    return int(dict(row)["cnt"]) if row else 0
