"""
設定ファイル読み込みモジュール（FreelanceScout 用シム）

shared.config を初期化し、各モジュールから from src.config import CONFIG で利用可能。
"""

from pathlib import Path

from shared.config import init_config, get  # noqa: F401

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "config.json"

CONFIG = init_config(_CONFIG_PATH)
