"""
市場別の最新価格日（営業日）の解決。

price_history と technical_snapshot の date を揃えないと流動性フィルタの JOIN が 0 件になる。
"""

from datetime import date

from src.data_store import fetch_one, get_connection

# 判定日として採用するのに必要な件数（最多日付の件数に対する比率）
_MIN_DATE_COVERAGE_RATIO = 0.5


def latest_price_date_for_market(target_date: date, market: str) -> date | None:
    """
    指定日以前で、当該市場の price_history に存在する最新の取引日を返す。
    データがなければ None。
    """
    target_str = target_date.strftime("%Y-%m-%d")
    with get_connection() as conn:
        row = fetch_one(
            conn,
            """SELECT MAX(p.date) AS d
               FROM price_history p
               JOIN universe_master u ON p.ticker = u.ticker
               WHERE u.market = ?
                 AND p.date <= ?
                 AND p.is_holiday = 0""",
            (market, target_str),
        )
    if not row or not row["d"]:
        return None
    return date.fromisoformat(str(row["d"]))


def resolve_snapshot_date(
    calendar_date: date,
    market: str,
    fetched_rows: list[dict] | None = None,
) -> date | None:
    """
    テクニカル・スクリーニングに使う日付を決める。

    1. 当日取得した価格行があれば、最多日付の半分以上の件数がある日付のうち最新の日
    2. なければ DB 上の最新価格日
    3. どちらもなければ None

    最多日付をそのまま使わないのは、yfinance が約定のない日の行を返さないため。
    取引の薄い銘柄ほど直近日の行が欠け、最多日付が 1〜2 営業日古い日になってしまう。
    半分未満の日付（誤って紛れ込んだ 1 件だけの日など）は採用しない。
    """
    if fetched_rows:
        from collections import Counter

        date_counts = Counter(r["date"] for r in fetched_rows if r.get("date"))
        if date_counts:
            threshold = max(date_counts.values()) * _MIN_DATE_COVERAGE_RATIO
            return date.fromisoformat(max(d for d, n in date_counts.items() if n >= threshold))
    return latest_price_date_for_market(calendar_date, market)
