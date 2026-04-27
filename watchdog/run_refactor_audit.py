"""
リファクタリング監査エントリーポイント

実行方法（watchdog/ から）:
    python run_refactor_audit.py
"""

import sys
from pathlib import Path

# shared パッケージを import できるようにリポジトリルートを追加
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from shared.config import init_config  # noqa: E402

# watchdog 専用の config.json を読み込む（Slack 設定等を共用）
_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
init_config(_CONFIG_PATH)

from refactor_audit import run  # noqa: E402


def main() -> int:
    try:
        return run()
    except Exception as e:
        print(f"[Refactor Audit] 致命的エラー: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
