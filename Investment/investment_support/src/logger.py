"""
ロギング設定モジュール（Investment 用再エクスポート）

shared.logger.get_logger を再エクスポートする。
各モジュールは従来どおり from src.logger import get_logger で利用可能。
"""

from shared.logger import get_logger  # noqa: F401
