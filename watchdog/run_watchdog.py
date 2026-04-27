"""
Watchdog エントリーポイント

実行方法（watchdog/ から）:
    python run_watchdog.py

タスクスケジューラでの登録例:
    プログラム: python
    引数:      run_watchdog.py
    開始:      C:\\Users\\bun22\\Desktop\\Ishikawa238\\watchdog
"""

import sys
from pathlib import Path

# shared パッケージを import できるようにリポジトリルートを追加
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from shared.config import init_config  # noqa: E402

# watchdog 専用の config.json を読み込む
_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
init_config(_CONFIG_PATH)

from watchdog import run  # noqa: E402


def main() -> int:
    try:
        return run()
    except Exception as e:
        print(f"[Watchdog] 致命的エラー: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
