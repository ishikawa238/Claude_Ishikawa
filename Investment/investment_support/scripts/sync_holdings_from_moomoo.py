"""
moomoo OpenD からポジションを取得して holdings.csv を更新するスクリプト

実行方法（investment_support/ から）:
    python -m scripts.sync_holdings_from_moomoo            # CSV 上書き
    python -m scripts.sync_holdings_from_moomoo --dry-run  # 上書きしない（確認のみ）

前提条件:
    - moomoo OpenD がローカルで起動していること
    - config.json に "moomoo" セクションがあること
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

from src.moomoo_sync import sync_from_moomoo
from src.holdings_monitor import sync_holdings_from_csv
from src.logger import get_logger

logger = get_logger(__name__)


def main():
    dry_run = "--dry-run" in sys.argv

    if dry_run:
        logger.info("=== moomoo ポジション同期 (dry-run) ===")
    else:
        logger.info("=== moomoo ポジション同期 ===")

    added, updated = sync_from_moomoo(dry_run=dry_run)

    if not dry_run and (added > 0 or updated > 0):
        logger.info("holdings.csv → DB 同期を実行中...")
        count = sync_holdings_from_csv()
        logger.info(f"DB 同期完了: {count} 件")

    logger.info(f"=== 完了（追加 {added} / 更新 {updated}）===")


if __name__ == "__main__":
    main()
