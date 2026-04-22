"""
設定ファイル読み込みモジュール（共有版）

各プロジェクトは起動時に init_config(path) を呼び出して設定を読み込む。
その後、get_config() で設定辞書を取得できる。
"""

import json
from pathlib import Path

_config: dict = {}


def init_config(config_path: Path | str) -> dict:
    """
    指定パスから config.json を読み込み、モジュールレベル辞書にセットする。
    各プロジェクトのエントリポイントで一度だけ呼び出す。
    返り値: 読み込んだ設定辞書
    """
    global _config
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"設定ファイルが見つかりません: {config_path}\n"
            "config_template.json をコピーして config.json を作成し、"
            "APIキーと各設定値を入力してください。"
        )
    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)
    _config.clear()
    _config.update(data)
    return _config


def get_config() -> dict:
    """
    読み込み済みの設定辞書を返す。
    init_config() が未呼び出しの場合は RuntimeError を送出する。
    """
    if not _config:
        raise RuntimeError(
            "shared.config.init_config() がまだ呼び出されていません。"
            "プロジェクトのエントリポイントで init_config(config_path) を呼び出してください。"
        )
    return _config


def get(section: str, key: str, default=None):
    """config[section][key] を安全に取得するヘルパー。"""
    return _config.get(section, {}).get(key, default)
