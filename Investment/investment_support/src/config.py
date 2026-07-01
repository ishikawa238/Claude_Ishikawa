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


def get_rs_score_config() -> dict:
    """rs_score セクションの設定辞書を返す。"""
    defaults = {
        "enabled": True,
        "lookback_days": 126,
        "jp_benchmark": "N225",
        "us_benchmark": "SP500",
        "min_rs_score": 70,
        "filter_enabled": False,
        "dynamic_rsi_enabled": True,
        "dynamic_rsi_base": 35,
        "dynamic_rsi_anchor_score": 70,
        "dynamic_rsi_slope": 0.5,
        "dynamic_rsi_max": 55,
    }
    user = CONFIG.get("rs_score", {}) or {}
    merged = {**defaults, **user}
    return merged


def get_breakout_config() -> dict:
    """breakout セクションの設定辞書を返す。"""
    defaults = {
        "enabled": True,
        "high_lookback_days": 60,
        "volume_ratio_min": 1.5,
        "max_candidates_per_batch": 1,
        "min_rs_score": None,
    }
    user = CONFIG.get("breakout", {}) or {}
    return {**defaults, **user}


def breakout_strategy_name(market: str) -> str:
    """市場別のブレイクアウト戦略名。"""
    return "breakout_jp" if market.upper() == "JP" else "breakout_us"


def is_breakout_enabled() -> bool:
    return bool(get_breakout_config().get("enabled", True))
