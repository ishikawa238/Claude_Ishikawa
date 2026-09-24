"""
投資委員会 判定の事後検証スクリプト

実行方法（investment_support/ ディレクトリから）:
    python -m scripts.committee_review                    # 全期間
    python -m scripts.committee_review --since 2026-10-01 # 指定日以降
    python -m scripts.committee_review --horizons 5 10 20 # 評価日数を変更
    python -m scripts.committee_review --detail           # 判定ごとの明細も表示

処理内容:
- committee_verdict_log の判定ごとに、判定後 N 営業日のリターンと損切り/利確の先着を集計
- 同期間の全エントリーセット（trade_signal_log）をベースラインとして並べて表示
"""

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

from src.committee_review import (
    DEFAULT_HORIZONS,
    evaluate_baseline,
    evaluate_verdicts,
    format_report,
)


def _fmt_ret(ret) -> str:
    return "-" if ret is None else f"{ret * 100:+.1f}%"


def main() -> None:
    parser = argparse.ArgumentParser(description="投資委員会の判定を事後の値動きで検証する")
    parser.add_argument("--since", default=None, help="集計開始日（YYYY-MM-DD）")
    parser.add_argument("--horizons", type=int, nargs="+", default=list(DEFAULT_HORIZONS),
                        help="評価する営業日数（既定: 5 20）")
    parser.add_argument("--detail", action="store_true", help="判定ごとの明細を表示")
    args = parser.parse_args()

    horizons = tuple(sorted(set(h for h in args.horizons if h > 0)))
    verdicts = evaluate_verdicts(args.since, horizons)
    baseline_since = args.since or (verdicts[0]["date"] if verdicts else None)
    baseline = evaluate_baseline(baseline_since, horizons)
    print(format_report(verdicts, baseline, horizons))

    if args.detail and verdicts:
        print("\n■ 明細")
        for v in verdicts:
            rets = " ".join(f"{h}日:{_fmt_ret(v[f'ret_{h}'])}" for h in horizons)
            print(f"  {v['date']} {v['ticker']:<8} {v['subject_type']:<13} {v['verdict']:<6} "
                  f"conf={v['confidence']} {rets} outcome={v['outcome'] or '-'}")


if __name__ == "__main__":
    main()
