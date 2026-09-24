"""
ロギング設定モジュール（共有版）

各モジュールは get_logger(__name__) でロガーを取得する。
ログはコンソールとファイル（logs/batch_YYYYMMDD.log）に同時出力される。
"""

import io
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from shared.config import get_config

# ログ出力先を config より優先して差し替える環境変数。
# テスト実行が本番の batch_YYYYMMDD.log を汚さないようにするために使う。
LOG_DIR_ENV = "SHARED_LOG_DIR"


def get_logger(name: str) -> logging.Logger:
    """指定名のロガーを返す。初回呼び出し時にハンドラを設定する。"""
    logger = logging.getLogger(name)

    # すでにハンドラが設定済みなら再設定しない
    if logger.handlers:
        return logger

    cfg = get_config()
    log_level_str = cfg.get("logging", {}).get("log_level", "INFO")
    log_level = getattr(logging, log_level_str.upper(), logging.INFO)
    logger.setLevel(log_level)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # コンソールハンドラ（Windows の cp932 環境でも文字化けしないよう utf-8 で出力）
    console_stream = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace"
    )
    console_handler = logging.StreamHandler(console_stream)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # ファイルハンドラ
    log_dir = Path(
        os.environ.get(LOG_DIR_ENV) or cfg.get("logging", {}).get("log_dir", "./logs")
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    log_file = log_dir / f"batch_{today}.log"

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
