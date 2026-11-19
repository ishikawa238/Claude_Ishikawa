"""
DB 初期化（english_radio/ から `python scripts/init_db.py`）。
何度実行しても安全（CREATE TABLE IF NOT EXISTS）。
"""

import _bootstrap

_bootstrap.setup()

from shared.logger import get_logger  # noqa: E402
from src import paths  # noqa: E402
from src.radio import ensure_db  # noqa: E402

if __name__ == "__main__":
    ensure_db()
    get_logger(__name__).info(f"DB を初期化しました: {paths.db_path()}")
