"""
設定ファイル読み込みモジュール（Investment 用シム）

shared.config を初期化し、既存コードとの後方互換性を維持する。
各モジュールは従来どおり from src.config import CONFIG で利用可能。
"""

from pathlib import Path

from shared.config import init_config, get  # noqa: F401

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "config.json"

CONFIG = init_config(_CONFIG_PATH)


def get_strategy_mode() -> str:
    """
    現在の戦略モードを返す（"defensive" / "offensive"）。
    未設定または不正値の場合は "defensive" を返す。
    """
    mode = str(CONFIG.get("strategy_mode", "defensive")).lower()
    return mode if mode in ("defensive", "offensive") else "defensive"


def is_offensive() -> bool:
    """攻めモードかどうかを返す。"""
    return get_strategy_mode() == "offensive"


def get_offensive_config() -> dict:
    """
    strategy.offensive の設定辞書を返す（未設定なら空辞書）。
    呼び出し側は .get() でデフォルト値を補完して使う。
    """
    return CONFIG.get("strategy", {}).get("offensive", {}) or {}
