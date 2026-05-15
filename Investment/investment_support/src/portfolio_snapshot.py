"""
ポートフォリオスナップショットモジュール

役割:
- バッチ実行時に総資産（保有時価 + 口座現金）をスナップショットとしてDBに保存する
- ダッシュボード表示用に最新スナップショットを取得する
"""

from datetime import date

from src.config import CONFIG
from src.data_store import fetch_all, fetch_one, get_connection
from src.logger import get_logger

logger = get_logger(__name__)

_CAP_CFG = CONFIG.get("capital", {})


def _fetch_moomoo_cash() -> dict[str, float]:
    """moomoo OpenD から口座現金残高を取得する。未接続時は 0.0 を返す。"""
    try:
        from src.moomoo_sync import fetch_account_cash
        cash = fetch_account_cash()
        return cash
    except Exception as e:
        logger.info(f"moomoo 現金残高取得スキップ: {e}")
        return {"cash_jpy": 0.0, "cash_usd": 0.0}


def _get_usdjpy_rate() -> float:
    """USD/JPY レートを取得する。取得失敗時はフォールバック値を返す。"""
    try:
        from src.yfinance_fetcher import fetch_usdjpy_rate
        rate = fetch_usdjpy_rate()
        if rate and rate > 0:
            return rate
    except Exception as e:
        logger.warning(f"USD/JPY レート取得失敗: {e}")
    fallback = float(_CAP_CFG.get("usdjpy_fallback", 150.0))
    logger.warning(f"USD/JPY フォールバック値使用: {fallback}")
    return fallback


def compute_and_save_snapshot(batch_type: str, target_date: date) -> dict:
    """
    ポートフォリオの総資産を計算し portfolio_snapshot に保存する。

    総資産 = 保有銘柄の時価合計 + 口座現金（moomoo）

    moomoo OpenD 未接続時は現金 = 0（保有時価のみ）となる。
    同日・同バッチタイプの再実行は上書きされる（冪等）。
    """
    capital_jpy = float(_CAP_CFG.get("total_capital_jpy", 0))
    capital_usd = float(_CAP_CFG.get("total_capital_usd", 0))
    date_str = target_date.isoformat() if isinstance(target_date, date) else str(target_date)

    with get_connection() as conn:
        # 保有銘柄を取得
        holdings = fetch_all(
            conn,
            """
            SELECT h.ticker, h.market, h.entry_price, h.quantity,
                   (SELECT adj_close FROM price_history
                    WHERE ticker = h.ticker AND date <= ? AND is_holiday = 0
                    ORDER BY date DESC LIMIT 1) AS current_price
            FROM holdings h
            WHERE h.is_closed = 0
            """,
            (date_str,),
        )

        # JP / US 別に集計
        holdings_value_jpy = 0.0
        holdings_value_usd = 0.0
        unrealized_pnl_jpy = 0.0
        unrealized_pnl_usd = 0.0
        count = 0

        for h in holdings:
            h = dict(h)  # sqlite3.Row → dict 変換
            ep = float(h["entry_price"] or 0)
            cp = float(h["current_price"] or 0) if h["current_price"] else 0
            qty = float(h["quantity"] or 1)
            if cp <= 0:
                logger.warning(f"価格未取得のためスナップショットから除外: {h['ticker']}")
                continue
            count += 1
            market_val = cp * qty
            pnl = (cp - ep) * qty
            if h.get("market", "JP") == "JP":
                holdings_value_jpy += market_val
                unrealized_pnl_jpy += pnl
            else:
                holdings_value_usd += market_val
                unrealized_pnl_usd += pnl

        # moomoo から口座現金残高を取得（未接続時は 0）
        moomoo_cash = _fetch_moomoo_cash()
        cash_jpy = moomoo_cash["cash_jpy"]
        cash_usd = moomoo_cash["cash_usd"]

        # 総資産 = 保有銘柄の時価合計 + 口座現金
        total_assets_jpy = holdings_value_jpy + cash_jpy
        total_assets_usd = holdings_value_usd + cash_usd

        # 円換算合計
        usdjpy = _get_usdjpy_rate()
        total_assets_combined_jpy = total_assets_jpy + total_assets_usd * usdjpy

        snapshot = {
            "snapshot_date": date_str,
            "batch_type": batch_type,
            "capital_jpy": capital_jpy,
            "capital_usd": capital_usd,
            "holdings_value_jpy": holdings_value_jpy,
            "holdings_value_usd": holdings_value_usd,
            "unrealized_pnl_jpy": unrealized_pnl_jpy,
            "unrealized_pnl_usd": unrealized_pnl_usd,
            "total_assets_jpy": total_assets_jpy,
            "total_assets_usd": total_assets_usd,
            "total_assets_combined_jpy": total_assets_combined_jpy,
            "cash_jpy": cash_jpy,
            "cash_usd": cash_usd,
            "usdjpy_rate": usdjpy,
            "holdings_count": count,
        }

        # INSERT OR REPLACE（UNIQUE(snapshot_date, batch_type) で冪等）
        conn.execute(
            """
            INSERT OR REPLACE INTO portfolio_snapshot
                (snapshot_date, batch_type,
                 capital_jpy, capital_usd,
                 holdings_value_jpy, holdings_value_usd,
                 unrealized_pnl_jpy, unrealized_pnl_usd,
                 total_assets_jpy, total_assets_usd,
                 total_assets_combined_jpy,
                 cash_jpy, cash_usd,
                 usdjpy_rate, holdings_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                date_str, batch_type,
                capital_jpy, capital_usd,
                holdings_value_jpy, holdings_value_usd,
                unrealized_pnl_jpy, unrealized_pnl_usd,
                total_assets_jpy, total_assets_usd,
                total_assets_combined_jpy,
                cash_jpy, cash_usd,
                usdjpy, count,
            ),
        )
        conn.commit()

    logger.info(
        f"ポートフォリオスナップショット保存: {date_str} ({batch_type}) "
        f"総資産 ¥{total_assets_combined_jpy:,.0f} "
        f"(JPY: ¥{total_assets_jpy:,.0f} / USD: ${total_assets_usd:,.2f})"
    )
    return snapshot


def get_latest_snapshot() -> dict | None:
    """最新のポートフォリオスナップショットを返す。存在しなければ None。"""
    with get_connection() as conn:
        row = fetch_one(
            conn,
            "SELECT * FROM portfolio_snapshot ORDER BY snapshot_date DESC, created_at DESC LIMIT 1",
        )
    return dict(row) if row else None
