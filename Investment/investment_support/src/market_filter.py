"""
市場全体スクリーニング・市場環境判定モジュール

役割:
- 流動性フィルタ（株価・売買代金・時価総額・非アクティブ除外）
- 市場環境モード判定（normal / caution / full_caution）
- フィルタ通過銘柄リストの返却
"""

from datetime import date
from typing import Optional

from src.config import CONFIG, get_swing_config
from src.data_store import fetch_all, fetch_one, get_connection, upsert
from src.logger import get_logger
from src.trading_calendar import latest_price_date_for_market

logger = get_logger(__name__)


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


_SC = CONFIG["screening"]
_JP_MIN_PRICE      = float(_SC["jp_min_price"])
_JP_MIN_TURNOVER   = float(_SC["jp_min_turnover"])
_JP_MIN_MARKET_CAP = float(_SC["jp_min_market_cap"])
_US_MIN_PRICE      = float(_SC["us_min_price"])
_US_MIN_TURNOVER   = float(_SC["us_min_turnover"])
_US_MIN_MARKET_CAP = float(_SC["us_min_market_cap"])
_MIN_ACTIVE_DAYS   = int(_SC["min_active_days_of_30"])
_ALLOW_UNKNOWN_MARKET_CAP = _as_bool(_SC.get("allow_unknown_market_cap", True))


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

def _price_band(market: str) -> tuple[float, float | None]:
    """市場別の株価帯（最低・最高）を返す。swing 設定を優先する。"""
    swing = get_swing_config()
    if market == "JP":
        min_p = float(swing.get("jp_min_price", _JP_MIN_PRICE))
        max_p = swing.get("jp_max_price")
        return min_p, float(max_p) if max_p is not None else None
    min_p = float(swing.get("us_min_price", _US_MIN_PRICE))
    max_p = swing.get("us_max_price")
    return min_p, float(max_p) if max_p is not None else None


def _liquidity_filter_sql(market: str) -> tuple[str, str]:
    """流動性フィルタ用 SELECT 文と時価総額条件句を返す。"""
    market_cap_condition = (
        "(t.est_market_cap IS NULL OR t.est_market_cap >= ?)"
        if _ALLOW_UNKNOWN_MARKET_CAP
        else "t.est_market_cap >= ?"
    )
    sql = f"""
        SELECT
            t.ticker, t.date, u.market, u.name,
            t.ma5, t.ma25, t.ma75, t.ma200,
            t.ema25, t.ema75,
            t.macd_diff, t.macd_dea, t.macd_hist,
            t.macd_diff_prev, t.macd_dea_prev, t.macd_hist_prev,
            t.ma5_5d_ago, t.ma25_5d_ago, t.ma200_5d_ago,
            t.vol_20d_avg, t.high_20d, t.high_52w, t.low_52w,
            t.return_5d, t.return_20d,
            t.rsi14, t.atr14, t.volatility_20d,
            t.est_turnover, t.est_market_cap,
            t.weekly_ma25, t.weekly_ma25_5w_ago,
            t.parabolic_sar, t.sar_direction,
            t.support_level, t.resistance_level,
            p.open, p.adj_close AS close, p.volume
        FROM technical_snapshot t
        JOIN universe_master u ON t.ticker = u.ticker
        JOIN price_history p   ON t.ticker = p.ticker AND t.date = p.date
        WHERE
            t.date    = ?
            AND u.market      = ?
            AND u.is_active   = 1
            AND u.is_excluded = 0
            AND t.ema25       IS NOT NULL
            AND t.ema75       IS NOT NULL
            AND p.adj_close   >= ?
            AND p.adj_close   <= ?
            AND t.est_turnover   >= ?
            AND {market_cap_condition}
    """
    return sql, market_cap_condition


def _fetch_liquidity_candidates(target_date: date, market: str) -> list[dict]:
    """非アクティブ除外前の流動性フィルタ通過候補。"""
    target_str = target_date.strftime("%Y-%m-%d")
    min_price, max_price = _price_band(market)
    if max_price is None:
        max_price = 1e12 if market == "US" else 1e9
    min_turnover   = _JP_MIN_TURNOVER   if market == "JP" else _US_MIN_TURNOVER
    min_market_cap = _JP_MIN_MARKET_CAP if market == "JP" else _US_MIN_MARKET_CAP
    sql, _ = _liquidity_filter_sql(market)

    with get_connection() as conn:
        rows = fetch_all(
            conn, sql,
            (target_str, market, min_price, max_price, min_turnover, min_market_cap),
        )
    return [dict(r) for r in rows]


def diagnose_screening_empty(target_date: date, market: str) -> Optional[str]:
    """
    流動性フィルタ通過が 0 件のとき、Slack/ログ用の原因メッセージを返す。
    テクニカル未計算など別要因のときは None。
    """
    target_str = target_date.strftime("%Y-%m-%d")
    with get_connection() as conn:
        snap = fetch_one(
            conn,
            """SELECT COUNT(*) AS c FROM technical_snapshot t
               JOIN universe_master u ON t.ticker = u.ticker
               WHERE t.date = ? AND u.market = ? AND u.is_active = 1""",
            (target_str, market),
        )
    snap_count = int(snap["c"]) if snap else 0
    if snap_count == 0:
        return None

    latest_price = latest_price_date_for_market(target_date, market)
    if latest_price and latest_price < target_date:
        return (
            f"⚠️ *スクリーニング日付不一致*（{market}）\n"
            f"テクニカルは {target_str} ですが、価格データの最新日は {latest_price.isoformat()} です。\n"
            "→ 夜バッチを再実行するか、土日の手動実行は避けてください（営業日に自動補正されます）。"
        )

    pre_rows = _fetch_liquidity_candidates(target_date, market)
    if not pre_rows:
        if not _ALLOW_UNKNOWN_MARKET_CAP:
            from src.shares_outstanding import count_shares_known
            known = count_shares_known(market)
            if known == 0:
                hint = (
                    "J-Quants API キー（JP）または月次バッチの EODHD 取得（US）で "
                    "発行済株式数を登録してください。"
                )
                return (
                    f"⚠️ *スクリーニング停止*（{market}）\n"
                    "「時価総額不明を許可」が OFF かつ、全銘柄で時価総額が未計算です。\n"
                    f"→ {hint}\n"
                    "→ 設定 `/settings` の「時価総額不明を許可」を ON にすると暫定復旧できます。"
                )
        return (
            f"⚠️ *スクリーニング異常*（{market} {target_str}）\n"
            f"テクニカル {snap_count:,} 件あるが、株価・売買代金・時価総額条件を満たす銘柄が 0 件です。"
        )

    filtered = _filter_inactive(pre_rows, target_date)
    if not filtered:
        return (
            f"⚠️ *スクリーニング異常*（{market} {target_str}）\n"
            f"流動性条件通過 {len(pre_rows):,} 件 → 非アクティブ除外後 0 件です。"
            f"（直近30日で {_MIN_ACTIVE_DAYS} 日未満取引の銘柄のみ残っています）"
        )
    return None


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
    rows = _fetch_liquidity_candidates(target_date, market)
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
