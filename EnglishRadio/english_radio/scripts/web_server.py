"""
Web 管理画面の起動（english_radio/ から `python scripts/web_server.py`）。

ブラウザで http://127.0.0.1:5210/ を開く。このPCからしか開けない（127.0.0.1 固定）。
"""

import _bootstrap

config_path = _bootstrap.setup()

from shared.config import get_config  # noqa: E402
from src.web.app import create_app  # noqa: E402

LOCAL_HOST = "127.0.0.1"


def main() -> None:
    port = int(get_config().get("web", {}).get("port", 5210))
    app = create_app(config_path)
    app.run(host=LOCAL_HOST, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
