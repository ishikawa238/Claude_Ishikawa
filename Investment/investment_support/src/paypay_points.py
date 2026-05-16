"""
PayPay ポイント運用トラッキングモジュール

役割:
- paypay_points.csv を読み込んで paypay_points テーブルに同期する
- コース別の損益率を計算する
- ポートフォリオスナップショットへのポイント残高提供
- 市場環境に基づくタイミングシグナル生成
"""

import csv
from datetime import date, timedelta
from pathlib import Path

from src.config import CONFIG
from src.data_store import fetch_all, fetch_one, get_connection
from src.logger import get_logger

logger = get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_POINTS_CSV = _PROJECT_ROOT / "paypay_points.csv"

# データ更新が3日以上前なら stale 扱い
_STALE_DAYS = 3

# コース → 連動先マッピング
# index: index_snapshot テーブルのコード（SP500 / NASDAQ）。None の場合は yf_ticker を使う。
# yf_ticker: yfinance のティッカー。Gold / 暗号資産 / 米国債のシグナル判定に使用。
PAYPAY_COURSES: dict[str, dict] = {
    "standard":       {"name": "スタンダード",             "index": "SP500",  "leverage": 1, "inverse": False, "yf_ticker": None},
    "technology":     {"name": "テクノロジー",             "index": "NASDAQ", "leverage": 1, "inverse": False, "yf_ticker": None},
    "challenge":      {"name": "チャレンジ",               "index": "SP500",  "leverage": 3, "inverse": False, "yf_ticker": None},
    "tech_challenge": {"name": "テクノロジーチャレンジ",   "index": "NASDAQ", "leverage": 3, "inverse": False, "yf_ticker": None},
    "inverse":        {"name": "逆チャレンジ",             "index": "SP500",  "leverage": 1, "inverse": True,  "yf_ticker": None},
    "tech_inverse":   {"name": "テクノロジー逆チャレンジ", "index": "NASDAQ", "leverage": 1, "inverse": True,  "yf_ticker": None},
    "gold":           {"name": "ゴールド",                 "index": None,     "leverage": 1, "inverse": False, "yf_ticker": "GLD"},
    "bitcoin":        {"name": "ビットコイン",             "index": None,     "leverage": 1, "inverse": False, "yf_ticker": "BTC-USD"},
    "ethereum":       {"name": "イーサリアム",             "index": None,     "leverage": 1, "inverse": False, "yf_ticker": "ETH-USD"},
    "inverse_bitcoin":  {"name": "逆ビットコイン",         "index": None,     "leverage": 1, "inverse": True,  "yf_ticker": "BTC-USD"},
    "inverse_ethereum": {"name": "逆イーサリアム",         "index": None,     "leverage": 1, "inverse": True,  "yf_ticker": "ETH-USD"},
    "us_treasury":    {"name": "米国債",                   "index": None,     "leverage": 3, "inverse": False, "yf_ticker": "TLT"},
}


# ============================================================
# CSV 読み込みと DB 同期
# ============================================================

def sync_paypay_points_from_csv() -> int:
    """
    paypay_points.csv を読み込んで paypay_points テーブルを同期する。
    CSV が存在しない場合は 0 を返す（エラーにしない）。
    返り値: 同期件数
    """
    if not _POINTS_CSV.exists():
        logger.info(f"paypay_points.csv が存在しないためスキップ: {_POINTS_CSV}")
        return 0

    rows: list[dict] = []
    with open(_POINTS_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for line in reader:
            course = line.get("course", "").strip()
            if not course:
                continue
            if course not in PAYPAY_COURSES:
                logger.warning(f"不明なコースのためスキップ: {course}")
                continue

            invested = _to_int(line.get("invested_points"))
            current = _to_int(line.get("current_points"))
            if invested is None or current is None:
                logger.warning(f"ポイント値が不正のためスキップ: {course}")
                continue

            last_updated = (line.get("last_updated") or "").strip()
            if not last_updated:
                last_updated = date.today().isoformat()

            rows.append({
                "course": course,
                "course_name": PAYPAY_COURSES[course]["name"],
                "invested_points": invested,
                "current_points": current,
                "last_updated": last_updated,
            })

    if rows:
        with get_connection() as conn:
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO paypay_points
                        (course, course_name, invested_points, current_points, last_updated, updated_at)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(course) DO UPDATE SET
                        course_name     = excluded.course_name,
                        invested_points = excluded.invested_points,
                        current_points  = excluded.current_points,
                        last_updated    = excluded.last_updated,
                        updated_at      = CURRENT_TIMESTAMP
                    """,
                    (row["course"], row["course_name"],
                     row["invested_points"], row["current_points"],
                     row["last_updated"]),
                )
            conn.commit()

    logger.info(f"PayPayポイント運用同期完了: {len(rows)} 件")
    return len(rows)


# ============================================================
# 評価
# ============================================================

def evaluate_paypay_points() -> list[dict]:
    """
    全コースのポイント運用状況を評価して返す。

    返り値リストの各要素:
    {
        course, course_name,
        invested_points, current_points,
        return_pct, return_points,
        last_updated, stale,
        index_code, leverage, inverse,
    }
    """
    with get_connection() as conn:
        rows = fetch_all(conn, "SELECT * FROM paypay_points ORDER BY course")

    today = date.today()
    result: list[dict] = []
    for r in rows:
        r = dict(r)
        invested = r["invested_points"]
        current = r["current_points"]
        return_points = current - invested
        return_pct = (return_points / invested * 100) if invested > 0 else 0.0

        last_updated = r["last_updated"]
        try:
            lu_date = date.fromisoformat(last_updated)
            stale = (today - lu_date).days >= _STALE_DAYS
        except (ValueError, TypeError):
            stale = True

        course_info = PAYPAY_COURSES.get(r["course"], {})
        result.append({
            "course": r["course"],
            "course_name": r["course_name"],
            "invested_points": invested,
            "current_points": current,
            "return_pct": round(return_pct, 1),
            "return_points": return_points,
            "last_updated": last_updated,
            "stale": stale,
            "index_code": course_info.get("index"),
            "leverage": course_info.get("leverage", 1),
            "inverse": course_info.get("inverse", False),
        })

    return result


def get_total_points_value() -> int:
    """全コースの current_points 合計を返す（1pt = 1円）。"""
    with get_connection() as conn:
        row = fetch_one(
            conn,
            "SELECT COALESCE(SUM(current_points), 0) AS total FROM paypay_points",
        )
    return int(row["total"]) if row else 0


# ============================================================
# タイミングシグナル生成
# ============================================================

def generate_points_signals(target_date: date) -> list[dict]:
    """
    市場環境に基づいてポイント運用のタイミングシグナルを生成する。

    ロジック:
    - インデックスが MA25 上 → 通常コースは good、逆コースは caution
    - インデックスが MA25 下 → 通常コースは caution、逆コースは good
    - 3倍レバは MA75 も参照し、より慎重に判定
    - Gold 等 yf_ticker のみのコースは _fetch_yf_ma25() で判定

    返り値: [{course, course_name, signal, reason}]
        signal: "good" / "neutral" / "caution"
    """
    # 保有中のコースのみ対象
    with get_connection() as conn:
        active_courses = fetch_all(conn, "SELECT course FROM paypay_points")
    active_set = {r["course"] for r in active_courses}
    if not active_set:
        return []

    # 市場環境を取得
    market_env = _get_market_env(target_date)

    signals: list[dict] = []
    for course_key in active_set:
        info = PAYPAY_COURSES.get(course_key)
        if not info:
            continue

        index_code = info["index"]
        leverage = info["leverage"]
        inverse = info["inverse"]
        name = info["name"]

        if index_code:
            # SP500 / NASDAQ — market_environment テーブルから判定
            sig, reason = _judge_index_signal(
                index_code, leverage, inverse, market_env,
            )
        elif info["yf_ticker"]:
            # Gold / BTC / ETH / TLT — yfinance MA25 で判定
            sig, reason = _judge_yf_signal(
                info["yf_ticker"], name, leverage, inverse, target_date,
            )
        else:
            continue

        signals.append({
            "course": course_key,
            "course_name": name,
            "signal": sig,
            "reason": reason,
        })

    return signals


# ============================================================
# 内部ヘルパー
# ============================================================

def _get_market_env(target_date: date) -> dict:
    """market_environment テーブルから最新行を取得する。"""
    date_str = target_date.isoformat()
    with get_connection() as conn:
        row = fetch_one(
            conn,
            "SELECT * FROM market_environment WHERE date <= ? ORDER BY date DESC LIMIT 1",
            (date_str,),
        )
    return dict(row) if row else {}


def _judge_index_signal(
    index_code: str,
    leverage: int,
    inverse: bool,
    market_env: dict,
) -> tuple[str, str]:
    """SP500 / NASDAQ 連動コースのシグナルを判定する。"""
    if index_code == "SP500":
        close = market_env.get("sp500_close")
        ma25 = market_env.get("sp500_ma25")
        index_label = "S&P500"
    else:
        # NASDAQ — market_environment には NASDAQ 列がないので index_snapshot から取得
        close, ma25 = _get_index_close_and_ma25(index_code)
        index_label = "NASDAQ"

    if close is None or ma25 is None:
        return "neutral", f"{index_label}のデータ不足"

    above_ma25 = close >= ma25

    if inverse:
        # 逆コース: 下落で有利
        if above_ma25:
            return "caution", f"{index_label} > MA25 — 逆コースには不利な地合い"
        else:
            return "good", f"{index_label} < MA25 — 逆コースに有利"

    # 通常コース
    if above_ma25:
        if leverage >= 3:
            # 3倍レバは MA75 も確認
            _, ma75 = _get_index_close_and_ma75(index_code)
            if ma75 and close >= ma75:
                return "good", f"{index_label} > MA25 & MA75 — レバレッジ追加OK"
            return "neutral", f"{index_label} > MA25 だが MA75 未満 — 3倍レバは様子見"
        return "good", f"{index_label} > MA25 — 追加タイミング良好"
    else:
        if leverage >= 3:
            return "caution", f"{index_label} < MA25 — 3倍レバのため追加控えめに"
        return "caution", f"{index_label} < MA25 — 追加は控えめに"


def _judge_yf_signal(
    yf_ticker: str,
    course_name: str,
    leverage: int,
    inverse: bool,
    target_date: date,
) -> tuple[str, str]:
    """yfinance ティッカー連動コースのシグナルを判定する。"""
    ma25 = _fetch_yf_ma25(yf_ticker, target_date)
    if ma25 is None:
        return "neutral", f"{course_name}: 価格データ取得失敗のため判定不可"

    close = _fetch_yf_latest_close(yf_ticker, target_date)
    if close is None:
        return "neutral", f"{course_name}: 最新価格取得失敗のため判定不可"

    above_ma25 = close >= ma25

    if inverse:
        if above_ma25:
            return "caution", f"{course_name}: 原資産 > MA25 — 逆コースには不利"
        return "good", f"{course_name}: 原資産 < MA25 — 逆コースに有利"

    if above_ma25:
        return "good", f"{course_name}: 原資産 > MA25 — 追加タイミング良好"
    return "caution", f"{course_name}: 原資産 < MA25 — 追加は控えめに"


def _get_index_close_and_ma25(index_code: str) -> tuple[float | None, float | None]:
    """index_snapshot テーブルから最新の close と ma25 を取得する。"""
    with get_connection() as conn:
        row = fetch_one(
            conn,
            "SELECT close, ma25 FROM index_snapshot "
            "WHERE index_code = ? AND close IS NOT NULL "
            "ORDER BY date DESC LIMIT 1",
            (index_code,),
        )
    if row:
        return row["close"], row["ma25"]
    return None, None


def _get_index_close_and_ma75(index_code: str) -> tuple[float | None, float | None]:
    """index_snapshot テーブルから最新の close と ma75 を取得する。"""
    with get_connection() as conn:
        row = fetch_one(
            conn,
            "SELECT close, ma75 FROM index_snapshot "
            "WHERE index_code = ? AND close IS NOT NULL "
            "ORDER BY date DESC LIMIT 1",
            (index_code,),
        )
    if row:
        return row["close"], row["ma75"]
    return None, None


def _fetch_yf_ma25(yf_ticker: str, target_date: date) -> float | None:
    """yfinance で直近60日分を取得し MA25 を計算する。取得失敗時は None。"""
    try:
        import yfinance as yf
        start = target_date - timedelta(days=90)
        df = yf.download(yf_ticker, start=start.isoformat(), end=target_date.isoformat(),
                         progress=False, auto_adjust=True)
        if df is None or len(df) < 25:
            return None
        return float(df["Close"].rolling(25).mean().dropna().iloc[-1])
    except Exception as e:
        logger.warning(f"yfinance MA25 取得失敗 ({yf_ticker}): {e}")
        return None


def _fetch_yf_latest_close(yf_ticker: str, target_date: date) -> float | None:
    """yfinance で最新終値を取得する。"""
    try:
        import yfinance as yf
        start = target_date - timedelta(days=7)
        df = yf.download(yf_ticker, start=start.isoformat(), end=target_date.isoformat(),
                         progress=False, auto_adjust=True)
        if df is None or len(df) == 0:
            return None
        return float(df["Close"].iloc[-1])
    except Exception as e:
        logger.warning(f"yfinance 最新価格取得失敗 ({yf_ticker}): {e}")
        return None


def _to_int(value: str | None) -> int | None:
    """文字列を int に変換する。空文字・None は None を返す。"""
    if not value or not str(value).strip():
        return None
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None
