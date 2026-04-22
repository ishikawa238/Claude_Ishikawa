"""
設定ファイル読み込みモジュール（Investment 用シム）

shared.config を初期化し、既存コードとの後方互換性を維持する。
各モジュールは従来どおり from src.config import CONFIG で利用可能。
"""

from pathlib import Path

from shared.config import init_config, get  # noqa: F401

# プロジェクトルートは本ファイルの2階層上
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "config.json"

# モジュールロード時に shared.config を初期化（後方互換）
CONFIG = init_config(_CONFIG_PATH)
