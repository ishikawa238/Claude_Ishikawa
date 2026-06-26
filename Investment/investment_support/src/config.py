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


def get_swing_config() -> dict:
    """swing セクションの設定辞書を返す。"""
    return CONFIG.get("swing", {}) or {}


def get_exit_simple_config() -> dict:
    """exit_simple セクションの設定辞書を返す。"""
    return CONFIG.get("exit_simple", {}) or {}


def get_market_config(market: str) -> dict:
    """markets.JP / markets.US の設定辞書を返す。"""
    markets = CONFIG.get("markets", {}) or {}
    key = market.lower()
    return markets.get(key, markets.get(market, {})) or {}


def is_market_enabled(market: str) -> bool:
    """市場のスクリーニングが有効かどうか。"""
    cfg = get_market_config(market)
    if "enabled" in cfg:
        return bool(cfg.get("enabled"))
    # 後方互換: markets 未設定時は JP/US とも有効
    return True


def get_market_strategy(market: str) -> str:
    """市場に割り当てられた戦略名（moomoo_swing / swing_jp）。"""
    cfg = get_market_config(market)
    if cfg.get("strategy"):
        return str(cfg["strategy"])
    return "moomoo_swing" if market.upper() == "US" else "swing_jp"


def get_strategy_mode() -> str:
    """後方互換: 常にスイング戦略。"""
    return "swing"


def is_offensive() -> bool:
    """後方互換: 攻めモードは廃止。"""
    return False


def get_offensive_config() -> dict:
    """後方互換: 空辞書を返す。"""
    return {}
