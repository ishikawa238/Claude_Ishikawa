"""
ペーパートレード更新スクリプト

実行方法（investment_support/ ディレクトリから）:
    python -m scripts.update_paper_trades               # 日次更新
    python -m scripts.update_paper_trades --summary      # サマリー表示のみ

処理内容:
- オープン中のペーパートレードを最新価格で評価
- 損切り・利確条件に達したものを自動クローズ
- --summary: 全ペーパートレードの統計を表示
"""

import argparse
import sys
from datetime import date
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

from src.logger import get_logger
from src.paper_trading import format_summary, get_paper_trade_summary, update_paper_trades

logger = get_logger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ペーパートレードの日次更新・サマリー表示"
    )
    parser.add_argument(
        "--summary", action="store_true",
        help="サマリーのみ表示（更新は行わない）"
    )
    parser.add_argument(
        "--date", default=None,
        help="評価日（YYYY-MM-DD、デフォルト: 今日）"
    )
    args = parser.parse_args()

    if args.summary:
        summary = get_paper_trade_summary()
        print(format_summary(summary))
        return

    target = date.today()
    if args.date:
        from datetime import datetime
        target = datetime.strptime(args.date, "%Y-%m-%d").date()

    logger.info(f"ペーパートレード日次更新: {target}")
    result = update_paper_trades(target)

    if result["closed"]:
        print(f"\n決済: {len(result['closed'])} 件")
        for t in result["closed"]:
            print(f"  {t['ticker']} — {t['exit_reason']}")

    print(f"オープン中: {len(result['open'])} 件")

    # サマリーも表示
    summary = get_paper_trade_summary()
    print(format_summary(summary))


if __name__ == "__main__":
    main()
