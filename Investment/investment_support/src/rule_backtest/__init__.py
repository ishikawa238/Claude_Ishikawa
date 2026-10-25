"""
現行ルールのバックテスト。

price_history から日ごとに指標とシグナルを再計算し、本番と同じ判定関数で
エントリー・損切り・exit_simple の出口を再現して損益を検証する。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .market_data import load_market_data
from .report import summarize
from .settings import BacktestSettings
from .signals import MarketFeed
from .simulator import PortfolioSimulator, SimulationResult


def load_feeds(
    markets: tuple[str, ...],
    start: str,
    end: str,
    db_path: str | Path | None = None,
) -> dict[str, MarketFeed]:
    """市場ごとのデータを読み込む（時間がかかるため、条件を変えた複数回の実行で使い回す）。"""
    return {m: MarketFeed(load_market_data(m, start, end, db_path)) for m in markets}


def simulate(settings: BacktestSettings, feeds: dict[str, MarketFeed]) -> tuple[SimulationResult, dict]:
    """読み込み済みのデータでシミュレーションと集計を行う。"""
    result = PortfolioSimulator(settings, feeds).run()
    benchmarks = {
        m: {
            "index": feeds[m].benchmark_index,
            "return_pct": feeds[m].benchmark_return_pct(),
            "yearly": feeds[m].benchmark_yearly_pct(),
        }
        for m in settings.markets
    }
    return result, summarize(result, benchmarks=benchmarks)


def run_rule_backtest(
    settings: BacktestSettings,
    db_path: str | Path | None = None,
) -> tuple[SimulationResult, dict]:
    """データ読み込み → シミュレーション → 集計を行い (結果, サマリー) を返す。"""
    feeds = load_feeds(settings.markets, settings.start, settings.end, db_path)
    return simulate(settings, feeds)


def compare_max_holdings(
    settings: BacktestSettings,
    feeds: dict[str, MarketFeed],
    values: list[int],
) -> list[dict]:
    """保有上限だけを変えて実行し、値ごとのサマリーを返す。"""
    return [simulate(replace(settings, max_holdings=v), feeds)[1] for v in values]


__all__ = [
    "BacktestSettings",
    "SimulationResult",
    "compare_max_holdings",
    "load_feeds",
    "run_rule_backtest",
    "simulate",
]
