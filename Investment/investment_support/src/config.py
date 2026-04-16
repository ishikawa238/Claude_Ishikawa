"""
設定ファイル読み込みモジュール
config.json を読み込み、各モジュールに設定値を提供する。
"""

import json
import os
from pathlib import Path


# プロジェクトルートは本ファイルの2階層上
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "config.json"


def load_config(config_path: Path = _CONFIG_PATH) -> dict:
    """
    config.json を読み込んで辞書で返す。
    ファイルが存在しない場合は FileNotFoundError を送出する。
    """
    if not config_path.exists():
        raise FileNotFoundError(
            f"設定ファイルが見つかりません: {config_path}\n"
            "config_template.json をコピーして config.json を作成し、"
            "APIキーと各設定値を入力してください。"
        )
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


# モジュールロード時に一度だけ読み込む（各モジュールは CONFIG を import して使う）
CONFIG = load_config()


def get(section: str, key: str, default=None):
    """config["section"]["key"] を安全に取得するヘルパー。"""
    return CONFIG.get(section, {}).get(key, default)
