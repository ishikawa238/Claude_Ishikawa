"""
スクリプト共通の初期化: sys.path 設定と config 読み込み。

english_radio/ を sys.path に入れて `src` を、リポジトリルートを入れて `shared` を import できるようにする。
カレントディレクトリも english_radio/ に揃え、ログ等の相対パスがずれないようにする。
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PROJECT_ROOT.parent.parent

for p in (str(PROJECT_ROOT), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.chdir(PROJECT_ROOT)

from shared.config import init_config  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config.json"


def setup() -> Path:
    init_config(CONFIG_PATH)
    return CONFIG_PATH
