"""
日付不一致の technical_snapshot を整理し、最新営業日でスクリーニング件数を確認する。

  python -m scripts.fix_screening_dates
"""
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT.parent.parent))
sys.path.insert(0, str(_ROOT))

from src.data_store import get_connection
from src.market_filter import get_screened_tickers
from src.trading_calendar import latest_price_date_for_market
from scripts.batch_evening_jp import _prune_jp_technical_orphans

_TODAY = date.today()


def main() -> None:
    latest = latest_price_date_for_market(_TODAY, "JP")
    print(f"calendar: {_TODAY}, latest JP price: {latest}")
    if _TODAY != latest and latest:
        _prune_jp_technical_orphans(_TODAY)
    work = latest or _TODAY
    screened = get_screened_tickers(work, "JP")
    print(f"screened tickers on {work}: {len(screened)}")


if __name__ == "__main__":
    main()
