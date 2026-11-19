"""テスト共通: shared パッケージを import できるようにリポジトリルートを sys.path に追加する。"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_PROJECT_ROOT), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
