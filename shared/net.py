"""ネットワーク関連の汎用ヘルパー。"""

import socket


def is_port_open(host: str, port: int, timeout: float = 3.0) -> bool:
    """host:port に TCP 接続できるかを timeout 秒以内に判定する。"""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False
