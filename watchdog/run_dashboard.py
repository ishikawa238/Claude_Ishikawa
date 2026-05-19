"""
Watchdog Dashboard エントリーポイント

実行方法（watchdog/ から）:
    python run_dashboard.py

タスクスケジューラでの登録例:
    プログラム: python
    引数:      run_dashboard.py
    開始:      C:\\Users\\bun22\\Desktop\\Ishikawa238\\watchdog
"""

import sys
from pathlib import Path

# shared パッケージを import できるようにリポジトリルートを追加
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from shared.config import get_config, init_config  # noqa: E402
from shared.logger import get_logger  # noqa: E402

# watchdog 専用の config.json を読み込む
_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
init_config(_CONFIG_PATH)

from dashboard import create_app  # noqa: E402

logger = get_logger(__name__)
app = create_app()


def main() -> int:
    cfg = get_config()
    dashboard_cfg = cfg.get("dashboard", {})
    host = dashboard_cfg.get("host", "127.0.0.1")
    port = dashboard_cfg.get("port", 5200)

    logger.info("Starting on http://%s:%s/", host, port)
    app.run(host=host, port=port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
