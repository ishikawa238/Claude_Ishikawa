"""
現行ルールのバックテスト実行スクリプト

価格履歴から日ごとにシグナルを再計算し、現行の売買ルール（スイング/ブレイクアウト検出・
エントリーセット・損切り・exit_simple・市場環境・運用停止ルール）で売買した場合の損益を検証する。
既定は本番と同じ日米合算（保有枠と運用停止ルールを共有、資金は市場ごと）。

実行方法（investment_support/ ディレクトリから）:
    python -m scripts.backtest_rules
    python -m scripts.backtest_rules --capital-jpy 100000 --capital-usd 1000 --compare-max-holdings 5,10,15,20
    python -m scripts.backtest_rules --market JP --start 2024-01-01 --end 2026-09-30

引数:
    --market                ALL（日米合算）/ JP / US                    既定: ALL
    --start / --end         期間（YYYY-MM-DD）                          既定: 2024-01-01 ～ 今日
    --capital-jpy           日本株の初期資金（円）                      既定: capital.total_capital_jpy
    --capital-usd           米国株の初期資金（ドル）                    既定: capital.total_capital_usd
    --usdjpy                円換算レート（固定）                        既定: capital.usdjpy_fallback
    --max-holdings          同時保有上限（全市場合計）                  既定: capital.max_concurrent_holdings
    --compare-max-holdings  保有上限を変えた比較（例: 5,10,15,20）
    --commission            片道手数料（%）                             既定: 0.1
    --slippage              片道スリッページ（%）                       既定: 0.1
    --lot-size              売買単位（株）                              既定: 1
    --sizing                fixed（初期資金基準）/ compound（評価額基準） 既定: fixed
    --stop-mode             intraday（安値で損切り）/ close（終値判定→翌寄付） 既定: intraday
    --no-risk-control       月間損失・連続損失による停止を無効にする
    --output-dir            結果の出力先                                既定: data/backtest/<実行日時>_<市場>
    --no-save               結果ファイルを書き出さない
"""

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

from src.logger import get_logger
from src.rule_backtest import BacktestSettings, compare_max_holdings, load_feeds, simulate
from src.rule_backtest.report import (
    format_by_strategy,
    format_holdings_comparison,
    format_monthly,
    format_summary,
    format_yearly,
    write_outputs,
)
from src.rule_backtest.settings import MARKETS

logger = get_logger(__name__)

# 候補ごと・日ごとの INFO ログが数千行になるため、バックテスト中は警告以上だけ出す
_QUIET_LOGGERS = ("src.signal_detector", "src.entry_calculator", "src.exit_simple", "src.rs_score")


def main() -> None:
    args = _parse_args()
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    markets = MARKETS if args.market == "ALL" else (args.market,)
    settings = BacktestSettings.from_config(
        markets,
        args.start,
        args.end,
        capital_jpy=args.capital_jpy,
        capital_usd=args.capital_usd,
        usdjpy=args.usdjpy,
        max_holdings=args.max_holdings,
        commission_pct=args.commission,
        slippage_pct=args.slippage,
        lot_size=args.lot_size,
        sizing=args.sizing,
        stop_mode=args.stop_mode,
        risk_control=False if args.no_risk_control else None,
    )
    logger.info(f"バックテスト開始: {settings}")
    feeds = load_feeds(settings.markets, settings.start, settings.end)
    result, summary = simulate(settings, feeds)

    for block in (format_summary, format_by_strategy, format_yearly, format_monthly):
        print(block(summary))
        print()

    comparison = None
    if args.compare_max_holdings:
        comparison = compare_max_holdings(settings, feeds, args.compare_max_holdings)
        print(format_holdings_comparison(comparison))

    if not args.no_save:
        out_dir = Path(args.output_dir) if args.output_dir else _default_output_dir(args.market)
        write_outputs(result, summary, out_dir)
        if comparison:
            with open(out_dir / "holdings_comparison.json", "w", encoding="utf-8") as f:
                json.dump(comparison, f, ensure_ascii=False, indent=2)
        print(f"\n結果ファイル: {out_dir}")


def _int_list(value: str) -> list[int]:
    try:
        return [int(v) for v in value.split(",") if v.strip()]
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"カンマ区切りの整数で指定してください: {value}") from e


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="現行ルールで過去の価格を売買した場合の損益を検証する")
    parser.add_argument("--market", default="ALL", choices=["ALL", *MARKETS])
    parser.add_argument("--start", default="2024-01-01", help="開始日（YYYY-MM-DD）")
    parser.add_argument("--end", default=date.today().isoformat(), help="終了日（YYYY-MM-DD）")
    parser.add_argument("--capital-jpy", type=float, default=None, help="日本株の初期資金（円）")
    parser.add_argument("--capital-usd", type=float, default=None, help="米国株の初期資金（ドル）")
    parser.add_argument("--usdjpy", type=float, default=None, help="円換算レート（固定）")
    parser.add_argument("--max-holdings", type=int, default=None, help="同時保有上限（全市場合計）")
    parser.add_argument("--compare-max-holdings", type=_int_list, default=None,
                        help="保有上限を変えて比較する値（例: 5,10,15,20）")
    parser.add_argument("--commission", type=float, default=0.1, help="片道手数料（%%）")
    parser.add_argument("--slippage", type=float, default=0.1, help="片道スリッページ（%%）")
    parser.add_argument("--lot-size", type=int, default=1, help="売買単位（株）")
    parser.add_argument("--sizing", default="fixed", choices=["fixed", "compound"])
    parser.add_argument("--stop-mode", default="intraday", choices=["intraday", "close"])
    parser.add_argument("--no-risk-control", action="store_true", help="運用停止ルールを無効にする")
    parser.add_argument("--output-dir", default=None, help="結果の出力先ディレクトリ")
    parser.add_argument("--no-save", action="store_true", help="結果ファイルを書き出さない")
    return parser.parse_args()


def _default_output_dir(market: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return _PROJECT_ROOT / "data" / "backtest" / f"{stamp}_{market}"


if __name__ == "__main__":
    main()
