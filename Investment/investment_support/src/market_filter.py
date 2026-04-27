"""
市場全体スクリーニング・市場環境判定モジュール

役割:
- 流動性フィルタ（株価・売買代金・時価総額・非アクティブ除外）
- 市場環境モード判定（normal / caution / full_caution）
- フィルタ通過銘柄リストの返却
"""

from datetime import date
from typing import Optional

from src.config import CONFIG
from src.data_store import fetch_all, fetch_one, get_connection, upsert
from src.logger import get_logger

logger = get_logger(__name__)

_SC = CONFIG["screening"]
_JP_MIN_PRICE      = float(_SC["jp_min_price"])
_JP_MIN_TURNOVER   = float(_SC["jp_min_turnover"])
_JP_MIN_MARKET_CAP = float(_SC["jp_min_market_cap"])
_US_MIN_PRICE      = float(_SC["us_min_price"])
_US_MIN_TURNOVER   = float(_SC["us_min_turnover"])
_US_MIN_MARKET_CAP = float(_SC["us_min_market_cap"])
_MIN_ACTIVE_DAYS   = int(_SC["min_active_days_of_30"])


# ============================================================
# 市場環境判定
# ============================================================

def judge_market_environment(target_date: date, market: str = "ALL") -> dict:
    """
    インデックスデータをもとに市場環境モードを判定して DB に保存する。

    引数:
        target_date: 判定対象日
        market: "JP" / "US" / "ALL"
                JP  → jp_above_ma25 のみ判定（夜バッチ用）
                US  → us_above_ma25 のみ判定（朝バッチ補完用）
                ALL → 両方判定して mode を確定

    返り値: market_environment テーブルの行に相当する辞書
    """
    target_str = target_date.strftime("%Y-%m-%d")

    # 既存レコードを取得（夜バッチ→朝バッチで2段階更新するため）
    with get_connection() as conn:
        existing = fetch_one(
            conn,
            "SELECT * FROM market_environment WHERE date = ?",
            (target_str,),
        )

    row = dict(existing) if existing else {
        "date":           target_str,
        "jp_above_ma25":  None,
        "us_above_ma25":  None,
        "mode":           "caution",
        "n225_close":     None,
        "n225_ma25":      None,
        "sp500_close":    None,
        "sp500_ma25":     None,
    }

    if market in ("JP", "ALL"):
        jp_data = _get_index_latest(target_str, "N225")
        if jp_data:
            row["n225_close"]    = jp_data["close"]
            row["n225_ma25"]     = jp_data["ma25"]
            row["jp_above_ma25"] = 1 if (jp_data["close"] or 0) >= (jp_data["ma25"] or 0) else 0
        else:
            logger.warning(f"N225データなし ({target_str}): jp_above_ma25 を None に設定")

    if market in ("US", "ALL"):
        us_data = _get_index_latest(target_str, "SP500")
        if us_data:
            row["sp500_close"]   = us_data["close"]
            row["sp500_ma25"]    = us_data["ma25"]
            row["us_above_ma25"] = 1 if (us_data["close"] or 0) >= (us_data["ma25"] or 0) else 0
        else:
            logger.warning(f"SP500データなし ({target_str}): us_above_ma25 を None に設定")

    # モード確定
    row["mode"] = _calc_mode(row["jp_above_ma25"], row["us_above_ma25"])

    # DB に保存（INSERT OR REPLACE）
    with get_connection() as conn:
        # id カラムを除いて upsert
        save_row = {k: v for k, v in row.items() if k != "id"}
        upsert(conn, "market_environment", [save_row])

    logger.info(
        f"市場環境判定: {target_str} → {row['mode']} "
        f"(JP={'↑' if row['jp_above_ma25'] else '↓' if row['jp_above_ma25'] == 0 else '?'} "
        f"US={'↑' if row['us_above_ma25'] else '↓' if row['us_above_ma25'] == 0 else '?'})"
    )
    return row


def _get_index_latest(target_str: str, index_code: str) -> Optional[dict]:
    """指定日以前の最新インデックスデータを取得する。"""
    with get_connection() as conn:
        row = fetch_one(
            conn,
            "SELECT close, ma25 FROM index_snapshot "
            "WHERE index_code = ? AND date <= ? AND close IS NOT NULL "
            "ORDER BY date DESC LIMIT 1",
            (index_code, target_str),
        )
    return dict(row) if row else None


def _calc_mode(jp: Optional[int], us: Optional[int]) -> str:
    """jp_above_ma25 / us_above_ma25 から mode 文字列を算出する。"""
    if jp == 1 and us == 1:
        return "normal"
    if jp == 0 and us == 0:
        return "full_caution"
    # どちらか一方が 0、または片方が None（データ未確定）
    if jp == 0 or us == 0:
        return "caution"
    # 両方 None（データ未取得）→ 安全のため caution
    return "caution"


def get_current_mode(target_date: date) -> str:
    """DB から指定日の市場環境モードを取得する。なければ 'caution' を返す。"""
    target_str = target_date.strftime("%Y-%m-%d")
    with get_connection() as conn:
        row = fetch_one(
            conn,
            "SELECT mode FROM market_environment WHERE date = ?",
            (target_str,),
        )
    return row["mode"] if row else "caution"


# ============================================================
# 流動性フィルタ
# ============================================================

def get_screened_tickers(target_date: date, market: str) -> list[dict]:
    """
    流動性フィルタを通過した銘柄のテクニカルスナップショットを返す。

    引数:
        target_date: 対象日
        market: "JP" または "US"

    返り値: technical_snapshot + universe_master の結合データ
            （フィルタ通過銘柄のみ）
    """
    target_str = target_date.strftime("%Y-%m-%d")

    min_price      = _JP_MIN_PRICE      if market == "JP" else _US_MIN_PRICE
    min_turnover   = _JP_MIN_TURNOVER   if market == "JP" else _US_MIN_TURNOVER
    min_market_cap = _JP_MIN_MARKET_CAP if market == "JP" else _US_MIN_MARKET_CAP

    # technical_snapshot と universe_master を結合してフィルタ適用
    sql = """
        SELECT
            t.ticker, t.date, u.market, u.name,
            t.ma5, t.ma25, t.ma75, t.ma200,
            t.ma5_5d_ago, t.ma25_5d_ago, t.ma200_5d_ago,
            t.vol_20d_avg, t.high_20d, t.high_52w, t.low_52w,
            t.return_5d, t.return_20d,
            t.rsi14, t.atr14, t.volatility_20d,
            t.est_turnover, t.est_market_cap,
            p.open, p.adj_close AS close, p.volume
        FROM technical_snapshot t
        JOIN universe_master u ON t.ticker = u.ticker
        JOIN price_history p   ON t.ticker = p.ticker AND t.date = p.date
        WHERE
            t.date    = ?
            AND u.market      = ?
            AND u.is_active   = 1
            AND u.is_excluded = 0
            AND t.ma200       IS NOT NULL
            AND p.adj_close   >= ?
            AND t.est_turnover   >= ?
            AND (t.est_market_cap IS NULL OR t.est_market_cap >= ?)
    """
    # est_market_cap が NULL の場合はフィルタをパスさせる
    # （発行済株式数が取得できない銘柄への対応）

    with get_connection() as conn:
        rows = fetch_all(
            conn, sql,
            (target_str, market, min_price, min_turnover, min_market_cap),
        )

    # sqlite3.Row → dict に変換（scoring.py 等で .get() を使うため）
    rows = [dict(r) for r in rows]

    # 非アクティブ（30日中20日未満取引）フィルタ
    filtered = _filter_inactive(rows, target_date)

    logger.info(
        f"流動性フィルタ: {market} {target_str} → "
        f"{len(rows)} 件中 {len(filtered)} 件通過"
    )
    return filtered


def _filter_inactive(rows: list, target_date: date) -> list:
    """
    直近30営業日のうち取引が存在する日が MIN_ACTIVE_DAYS 未満の銘柄を除外する。
    price_history の volume > 0 の日数で判定する。
    """
    if not rows:
        return []

    from datetime import timedelta
    from_str   = (target_date - timedelta(days=50)).strftime("%Y-%m-%d")
    target_str = target_date.strftime("%Y-%m-%d")

    tickers = [r["ticker"] for r in rows]
    # 一括で全銘柄の取引日数を取得
    placeholders = ",".join(["?"] * len(tickers))
    sql = f"""
        SELECT ticker, COUNT(*) as active_days
        FROM price_history
        WHERE ticker IN ({placeholders})
          AND date BETWEEN ? AND ?
          AND volume > 0
          AND is_holiday = 0
        GROUP BY ticker
    """
    with get_connection() as conn:
        counts = fetch_all(conn, sql, tuple(tickers) + (from_str, target_str))

    active_map = {r["ticker"]: r["active_days"] for r in counts}
    result = [r for r in rows if active_map.get(r["ticker"], 0) >= _MIN_ACTIVE_DAYS]
    return result
