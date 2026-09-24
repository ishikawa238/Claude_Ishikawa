"""Investment テストパッケージ。

テスト実行時の共通 sys.path セットアップを行う。
`py -m unittest tests.test_xxx` のように直接モジュールを指定して実行した
場合でも、`shared.*` など repo ルート配下のモジュールを import できるよう、
プロジェクトルートと repo ルートを sys.path に追加する。

`unittest discover` 経由で実行した場合も同じパスがロードされる。
"""

import os
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent

for _p in (str(_REPO_ROOT), str(_PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# テストのログを本番の logs/batch_YYYYMMDD.log に混ぜないよう、一時ディレクトリへ逃がす。
# shared.logger は初回の get_logger() 時にこの環境変数を読む。
os.environ.setdefault(
    "SHARED_LOG_DIR", str(Path(tempfile.gettempdir()) / "ishikawa_test_logs")
)
