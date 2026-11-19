"""
朝の英語ラジオ（タスクスケジューラから実行されるエントリポイント）。

使い方（english_radio/ から）:
    python scripts/morning_radio.py            # 取得 → 再生 → Slack 通知
    python scripts/morning_radio.py --no-play  # 取得と選曲だけ（再生・通知なし）
    python scripts/morning_radio.py --dry-run  # ダウンロードせず、通知はコンソールに表示
"""

import argparse
import sys

import _bootstrap

_bootstrap.setup()

from src.radio import RunOptions, run  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="朝の英語ラジオ")
    parser.add_argument("--no-play", action="store_true", help="再生しない")
    parser.add_argument("--dry-run", action="store_true", help="ダウンロードせず通知も送らない")
    parser.add_argument("--trigger", default="schedule", choices=["schedule", "manual", "web"])
    args = parser.parse_args()

    try:
        run(RunOptions(play=not args.no_play, dry_run=args.dry_run, trigger=args.trigger))
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
