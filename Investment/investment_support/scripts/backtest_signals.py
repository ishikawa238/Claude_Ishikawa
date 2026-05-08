"""
バックテスト実行スクリプト

実行方法（investment_support/ ディレクトリから）:
    python -m scripts.backtest_signals --start 2024-01-01 --end 2026-04-30
    python -m scripts.backtest_signals --start 2024-01-01 --end 2026-04-30 --market JP --capital 200000

引数:
    --start   開始日（YYYY-MM-DD）  必須
    --end     終了日（YYYY-MM-DD）  必須
    --market  市場（JP / US / ALL） デフォルト: ALL
    --capital 初期資金              デフォルト: config.json の total_capital_jpy
    --commission  片道手数料（%）    デフォルト: 0.1
    --slippage    スリッページ（%）  デフォルト: 0.1
"""

import argparse
import sys
from pathlib import Path

# プロジェクトルートを sys.path に追加
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

from src.backtester import format_summary, run_backtest, summarize_results
from src.config import CONFIG
from src.logger import get_logger

logger = get_logger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="過去のシグナルデータを使ってバックテストを実行する"
    )
    parser.add_argument("--start", required=True, help="開始日（YYYY-MM-DD）")
    parser.add_argument("--end", required=True, help="終了日（YYYY-MM-DD）")
    parser.add_argument(
        "--market", default="ALL", choices=["JP", "US", "ALL"],
        help="対象市場（デフォルト: ALL）"
    )
    parser.add_argument(
        "--capital", type=float, default=None,
        help="初期資金（デフォルト: config.json の total_capital_jpy）"
    )
    parser.add_argument(
        "--commission", type=float, default=0.1,
        help="片道手数料（%%、デフォルト: 0.1）"
    )
    parser.add_argument(
        "--slippage", type=float, default=0.1,
        help="スリッページ（%%、デフォルト: 0.1）"
    )

    args = parser.parse_args()

    capital = args.capital
    if capital is None:
        capital = float(CONFIG["capital"]["total_capital_jpy"])

    logger.info(f"バックテスト実行: {args.start} ～ {args.end}")

    trades = run_backtest(
        start_date=args.start,
        end_date=args.end,
        market=args.market,
        capital=capital,
        commission_pct=args.commission,
        slippage_pct=args.slippage,
    )

    summary = summarize_results(trades, initial_capital=capital)
    print(format_summary(summary, initial_capital=capital))

    # 個別トレードの一覧
    if trades:
        print("\n--- トレード一覧 ---")
        print(f"{'Ticker':<12} {'Pattern':>3} {'Entry':>10} {'Exit':>10} "
              f"{'PnL%':>7} {'PnL':>10} {'Reason':<12}")
        print("-" * 70)
        for t in sorted(trades, key=lambda x: x["entry_date"]):
            print(
                f"{t['ticker']:<12} {t['pattern']:>3} "
                f"{t['entry_date']:>10} {t['exit_date']:>10} "
                f"{t['pnl_pct']:>+6.2f}% {t['pnl_amount']:>+10,.0f} "
                f"{t['exit_reason']:<12}"
            )


if __name__ == "__main__":
    main()
