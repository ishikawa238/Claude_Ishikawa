"""
EnglishRadio のパス解決。

config.json に書かれた相対パスは、実行時のカレントディレクトリではなく
english_radio/ フォルダを基準に解決する（タスクスケジューラ経由でも同じ場所を指すため）。
"""

from pathlib import Path

from shared.config import get_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PROJECT_ROOT.parent.parent

_DEFAULTS = {
    "db_path": "./data/english_radio.db",
    "library_dir": "./library",
    "data_dir": "./data",
}


def resolve(path_str: str) -> Path:
    """PROJECT_ROOT 基準で絶対パスに変換する。"""
    path = Path(path_str)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def get_path(key: str) -> Path:
    """config の paths セクション（無ければ既定値）からパスを返す。"""
    paths_cfg = get_config().get("paths", {})
    return resolve(paths_cfg.get(key, _DEFAULTS[key]))


def db_path() -> Path:
    return get_path("db_path")


def library_dir() -> Path:
    return get_path("library_dir")


def data_dir() -> Path:
    return get_path("data_dir")


def log_dir() -> Path:
    log_dir_str = get_config().get("logging", {}).get("log_dir", "./logs")
    return resolve(log_dir_str)


def is_inside(path: Path, parent: Path) -> bool:
    """path が parent 配下にあるかを判定する（パストラバーサル対策）。"""
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
