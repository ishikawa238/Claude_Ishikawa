"""
学習バッチ（タスクスケジューラから毎朝 09:00 に実行）。

使い方（english_radio/ から）:
    python scripts/study_batch.py               # 未作成のエピソードを文字起こし → 和訳・単語抽出
    python scripts/study_batch.py --guid <guid> # 1話だけ
    python scripts/study_batch.py --dry-run     # 何を処理するかだけ表示（実行ログに残さない）
"""

import argparse
import sys

import _bootstrap

_bootstrap.setup()

from src.study.runner import StudyOptions, run  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="英語ラジオの学習バッチ（文字起こし・和訳・単語）")
    parser.add_argument("--guid", help="この1話だけ処理する")
    parser.add_argument("--dry-run", action="store_true", help="処理予定を表示するだけ")
    args = parser.parse_args()
    try:
        run(StudyOptions(guid=args.guid, dry_run=args.dry_run))
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
