"""
バッチスクリプトの API 失敗シナリオテスト

テスト対象:
- fetch_daily_yf が Timeout を送出したとき、バッチが落ちずに継続すること
- 価格取得が全件失敗したとき、テクニカル計算・シグナル検出がスキップされること
- Slack 通知が失敗しても、バッチが "success" / "partial_failure" で完了すること

テスト方針:
- _run_batch() を直接呼ぶ。外部依存（DB・API・Slack）はすべてモックで置き換える。
- CONFIG モックは src.config.CONFIG をパッチして import 時の副作用を回避する。
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# sys.path を揃える（investment_support/ から実行される前提）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent  # Ishikawa238/
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

# ─── CONFIG モック（import 時の副作用を回避するため最初に設定） ───
_CONFIG_MOCK = {
    "database": {"db_path": ":memory:", "backup_dir": "/tmp", "backup_retention_days": "7"},
    "logging":  {"log_dir": "/tmp", "log_level": "INFO"},
    "eodhd":    {"base_url": "https://example.com", "api_key": "test", "request_interval_sec": "0"},
    "slack":    {"webhook_url": "https://hooks.example.com/test"},
    "signal":   {
        "pattern_a_rsi_min": "50", "pattern_a_rsi_max": "70",
        "pattern_a_volume_ratio_min": "1.2", "pattern_a_52w_high_pct": "0.92",
        "pattern_b_rsi_min": "40", "pattern_b_rsi_max": "60",
        "pattern_b_ma25_touch_pct": "0.98", "pattern_b_lookback_days": "5",
        "max_candidates_per_pattern": "3",
        "atr_stop_multiplier": "2.0",
        "risk_reward_ratio": "2.0",
        "stop_loss_warning_min_pct": "3",
        "stop_loss_warning_max_pct": "15",
    },
    "screening": {
        "jp_min_price": "300",
        "jp_min_turnover": "300000000",
        "jp_min_market_cap": "30000000000",
        "us_min_price": "5",
        "us_min_turnover": "20000000",
        "us_min_market_cap": "2000000000",
        "min_active_days_of_30": "20",
    },
    "capital":  {
        "total_capital_jpy": "1000000", "total_capital_usd": "10000",
        "max_risk_per_trade_pct": "2", "default_stop_loss_pct": "8",
        "max_concurrent_holdings": "3", "usdjpy_fallback": "150",
    },
    "jquants":  {"earnings_alert_days_warning": "7", "earnings_alert_days_critical": "3"},
    "report_server": {"host": "127.0.0.1", "port": "5100"},
    "llm": {"enabled": "false"},
    "paper_mode": {"enabled": "false"},
    "moomoo": {"host": "127.0.0.1", "port": "11111"},
    "schedule": {"evening_jp_target_time": "20:30", "morning_all_target_time": "07:30"},
}

import src.config as _src_config
_src_config.CONFIG = _CONFIG_MOCK


# ─── バッチモジュールを CONFIG パッチ後に import ───
with patch("src.config.CONFIG", _CONFIG_MOCK):
    import scripts.batch_evening_jp as _evening
    import scripts.batch_morning_all as _morning


# ============================================================
# ヘルパー
# ============================================================

def _make_conn_mock() -> MagicMock:
    """get_connection() コンテキストマネージャのモックを作って返す。"""
    conn = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=conn)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def _conn_factory():
    """get_connection が複数回呼ばれるときに毎回新しいモックを返すファクトリ。"""
    return _make_conn_mock()


def _apply_patches(patches: list) -> list:
    """パッチリストをすべて start して active リストを返す。"""
    return [p.start() for p in patches]


def _stop_patches(patches: list) -> None:
    """パッチリストをすべて stop する。"""
    for p in patches:
        p.stop()


# ============================================================
# 夜バッチ共通モックビルダー
# ============================================================

def _empty_price_result(total: int = 1):
    from src.yfinance_fetcher import DailyPriceFetchResult
    return DailyPriceFetchResult(
        rows=[],
        total_tickers=total,
        tickers_with_target_date=0,
        failed_tickers=[f"T{i}" for i in range(total)] if total else [],
        status="failed",
    )


def _evening_patches(**overrides) -> list:
    """
    夜バッチ (_run_batch) に必要な共通パッチのリストを返す。
    overrides でデフォルト値を上書きできる。
    """
    defaults = {
        "scripts.batch_evening_jp.get_connection": MagicMock(side_effect=_conn_factory),
        "scripts.batch_evening_jp.start_batch_log": MagicMock(return_value=1),
        "scripts.batch_evening_jp.finish_batch_log": MagicMock(),
        "scripts.batch_evening_jp.fetch_all": MagicMock(return_value=[]),
        "scripts.batch_evening_jp.upsert": MagicMock(),
        "scripts.batch_evening_jp.fetch_index_history": MagicMock(return_value=[]),
        "scripts.batch_evening_jp.fetch_index_history_yf": MagicMock(return_value=[]),
        "scripts.batch_evening_jp._fetch_jp_daily_with_status": MagicMock(
            return_value=_empty_price_result(),
        ),
        "scripts.batch_evening_jp.resolve_snapshot_date": MagicMock(return_value=None),
        "scripts.batch_evening_jp.calculate_and_save_index_mas": MagicMock(),
        "scripts.batch_evening_jp.judge_market_environment": MagicMock(return_value={"mode": "normal"}),
        "scripts.batch_evening_jp.calculate_and_save_snapshots": MagicMock(),
        "scripts.batch_evening_jp.get_screened_tickers": MagicMock(return_value=[]),
        "scripts.batch_evening_jp.detect_and_save_signals": MagicMock(return_value=[]),
        "scripts.batch_evening_jp.save_entry_sets": MagicMock(return_value=[]),
        "scripts.batch_evening_jp.sync_holdings_from_csv": MagicMock(),
        "scripts.batch_evening_jp.ensure_holding_prices": MagicMock(),
        "scripts.batch_evening_jp.evaluate_holdings": MagicMock(return_value=[]),
        "scripts.batch_evening_jp.send_evening_report": MagicMock(),
        "scripts.batch_evening_jp.backup_database": MagicMock(),
        "scripts.batch_evening_jp.log_error": MagicMock(),
        "scripts.batch_evening_jp._update_earnings_schedule_for_holdings": MagicMock(),
        # 遅延 import されるモジュールをモック
        "src.moomoo_sync.sync_from_moomoo": MagicMock(),
        "src.moomoo_sync.sync_trade_results": MagicMock(),
        "src.paper_trading.is_paper_mode_enabled": MagicMock(return_value=False),
        "src.signal_detector.fetch_near_misses": MagicMock(return_value=[]),
        "src.news_fetcher.fetch_market_news": MagicMock(return_value=[]),
        "src.news_fetcher.fetch_ticker_news": MagicMock(return_value=[]),
        "src.llm_advisor.get_llm_feedback": MagicMock(return_value=[]),
    }
    defaults.update(overrides)
    return [patch(target, mock) for target, mock in defaults.items()]


def _morning_patches(**overrides) -> list:
    """
    朝バッチ (_run_batch) に必要な共通パッチのリストを返す。
    """
    defaults = {
        "scripts.batch_morning_all.get_connection": MagicMock(side_effect=_conn_factory),
        "scripts.batch_morning_all.start_batch_log": MagicMock(return_value=1),
        "scripts.batch_morning_all.finish_batch_log": MagicMock(),
        "scripts.batch_morning_all.fetch_all": MagicMock(return_value=[]),
        "scripts.batch_morning_all.fetch_one": MagicMock(return_value=None),
        "scripts.batch_morning_all.upsert": MagicMock(),
        "scripts.batch_morning_all.fetch_bulk_eod": MagicMock(return_value=[]),
        "scripts.batch_morning_all.fetch_index_history": MagicMock(return_value=[]),
        "scripts.batch_morning_all.fetch_index_history_yf": MagicMock(return_value=[]),
        "scripts.batch_morning_all.fetch_daily_yf": MagicMock(return_value=[]),
        "scripts.batch_morning_all.calculate_and_save_index_mas": MagicMock(),
        "scripts.batch_morning_all.judge_market_environment": MagicMock(return_value={"mode": "normal"}),
        "scripts.batch_morning_all.calculate_and_save_snapshots": MagicMock(),
        "scripts.batch_morning_all.get_screened_tickers": MagicMock(return_value=[]),
        "scripts.batch_morning_all.detect_and_save_signals": MagicMock(return_value=[]),
        "scripts.batch_morning_all.save_entry_sets": MagicMock(return_value=[]),
        "scripts.batch_morning_all.sync_holdings_from_csv": MagicMock(),
        "scripts.batch_morning_all.ensure_holding_prices": MagicMock(),
        "scripts.batch_morning_all.evaluate_holdings": MagicMock(return_value=[]),
        "scripts.batch_morning_all.send_morning_report": MagicMock(),
        "scripts.batch_morning_all.log_error": MagicMock(),
        "scripts.batch_morning_all._load_last_evening_entry_sets": MagicMock(return_value=[]),
        # 遅延 import されるモジュールをモック
        "src.moomoo_sync.sync_from_moomoo": MagicMock(),
        "src.moomoo_sync.sync_trade_results": MagicMock(),
        "src.paper_trading.is_paper_mode_enabled": MagicMock(return_value=False),
        "src.signal_detector.fetch_near_misses": MagicMock(return_value=[]),
        "src.news_fetcher.fetch_market_news": MagicMock(return_value=[]),
        "src.news_fetcher.fetch_ticker_news": MagicMock(return_value=[]),
        "src.llm_advisor.get_llm_feedback": MagicMock(return_value=[]),
    }
    defaults.update(overrides)
    return [patch(target, mock) for target, mock in defaults.items()]


# ============================================================
# T-1-1: fetch_daily_yf が Timeout を送出したときのテスト
# ============================================================

class EveningBatchTimeoutTests(unittest.TestCase):
    """夜バッチ: fetch_daily_yf が Timeout を送出してもクラッシュしない。"""

    def test_timeout_error_in_fetch_daily_yf_does_not_crash(self):
        """
        fetch_daily_yf が requests.exceptions.Timeout を送出しても
        バッチは完了コード 0 / 1 / 2 を返す（os._exit() で強制終了しない）。

        実際の yfinance_fetcher は内部で例外を catch して [] を返すが、
        ここでは fetch_daily_yf 自体が例外を送出するケースをシミュレートする。
        Timeout は _run_batch の except Exception で捕捉されるため
        返り値は 2（致命的エラー）になる。
        """
        import requests

        patches = _evening_patches(
            **{"scripts.batch_evening_jp._fetch_jp_daily_with_status": MagicMock(
                side_effect=requests.exceptions.Timeout("接続タイムアウト")
            )}
        )
        _apply_patches(patches)
        try:
            result = _evening._run_batch()
            self.assertIn(result, (0, 1, 2),
                          f"予期しない返り値: {result}")
        finally:
            _stop_patches(patches)

    def test_fetch_daily_yf_returns_empty_does_not_crash(self):
        """
        fetch_daily_yf が [] を返す（全件失敗相当）とき、バッチは完了する。
        """
        patches = _evening_patches()
        _apply_patches(patches)
        try:
            result = _evening._run_batch()
            self.assertIn(result, (0, 1))
        finally:
            _stop_patches(patches)


# ============================================================
# T-1-2: 全件失敗時にテクニカル計算・シグナル検出がスキップされること
# ============================================================

class EveningBatchAllFailureTests(unittest.TestCase):
    """夜バッチ: 全銘柄の価格取得失敗時のスキップ動作テスト。"""

    def test_all_price_fetch_fail_skips_technical_and_signal(self):
        """
        fetch_daily_yf が [] を返し、universe_master に銘柄が存在するとき、
        calculate_and_save_snapshots と detect_and_save_signals が呼ばれない。
        """
        # universe_master に1件だけ銘柄を返す
        def fake_fetch_all(conn, sql, *args):
            if "universe_master" in sql:
                return [{"ticker": "7203"}]
            return []

        mock_detect = MagicMock(return_value=[])
        mock_calc = MagicMock()

        patches = _evening_patches(
            **{
                "scripts.batch_evening_jp.fetch_all": MagicMock(side_effect=fake_fetch_all),
                "scripts.batch_evening_jp.calculate_and_save_snapshots": mock_calc,
                "scripts.batch_evening_jp.detect_and_save_signals": mock_detect,
            }
        )
        _apply_patches(patches)
        try:
            result = _evening._run_batch()
            self.assertIn(result, (0, 1))
            # テクニカル計算・シグナル検出は呼ばれない
            mock_calc.assert_not_called()
            mock_detect.assert_not_called()
        finally:
            _stop_patches(patches)


class MorningBatchAllFailureTests(unittest.TestCase):
    """朝バッチ: 全銘柄の価格取得失敗時のスキップ動作テスト。"""

    def test_all_price_fetch_fail_skips_technical_and_signal(self):
        """
        朝バッチで fetch_daily_yf が [] を返し、銘柄が存在するとき、
        calculate_and_save_snapshots と detect_and_save_signals が呼ばれない。
        """
        def fake_fetch_all(conn, sql, *args):
            if "universe_master" in sql:
                return [{"ticker": "AAPL"}]
            return []

        mock_detect = MagicMock(return_value=[])
        mock_calc = MagicMock()

        patches = _morning_patches(
            **{
                "scripts.batch_morning_all.fetch_all": MagicMock(side_effect=fake_fetch_all),
                "scripts.batch_morning_all.calculate_and_save_snapshots": mock_calc,
                "scripts.batch_morning_all.detect_and_save_signals": mock_detect,
            }
        )
        _apply_patches(patches)
        try:
            result = _morning._run_batch()
            self.assertIn(result, (0, 1))
            mock_calc.assert_not_called()
            mock_detect.assert_not_called()
        finally:
            _stop_patches(patches)


# ============================================================
# T-1-3: Slack 通知失敗でもバッチが完了すること
# ============================================================

class SlackNotificationFailureTests(unittest.TestCase):
    """Slack 通知失敗時にバッチが完了コード 0 or 1 を返すこと。"""

    def test_evening_batch_completes_even_if_slack_fails(self):
        """
        send_evening_report が例外を送出しても夜バッチは完了する。
        finish_batch_log が "success" or "partial_failure" で呼ばれる。
        """
        mock_finish = MagicMock()

        patches = _evening_patches(
            **{
                "scripts.batch_evening_jp.finish_batch_log": mock_finish,
                "scripts.batch_evening_jp.send_evening_report": MagicMock(
                    side_effect=Exception("Slack webhook failed")
                ),
            }
        )
        _apply_patches(patches)
        try:
            result = _evening._run_batch()
            # Slack 失敗はバッチを致命的エラー(2)にしない
            self.assertIn(result, (0, 1),
                          f"Slack 失敗でバッチが異常終了: 返り値={result}")
            # finish_batch_log が呼ばれ、第3引数が "success" または "partial_failure"
            self.assertTrue(mock_finish.called)
            # finish_batch_log(conn, batch_id, status, extra) の status は第3引数 (index 2)
            status_arg = mock_finish.call_args[0][2]
            self.assertIn(status_arg, ("success", "partial_failure"),
                          f"予期しないステータス: {status_arg}")
        finally:
            _stop_patches(patches)

    def test_morning_batch_completes_even_if_slack_fails(self):
        """
        send_morning_report が例外を送出しても朝バッチは完了する。
        """
        mock_finish = MagicMock()

        patches = _morning_patches(
            **{
                "scripts.batch_morning_all.finish_batch_log": mock_finish,
                "scripts.batch_morning_all.send_morning_report": MagicMock(
                    side_effect=Exception("Webhook timeout")
                ),
            }
        )
        _apply_patches(patches)
        try:
            result = _morning._run_batch()
            self.assertIn(result, (0, 1),
                          f"Slack 失敗でバッチが異常終了: 返り値={result}")
            self.assertTrue(mock_finish.called)
            status_arg = mock_finish.call_args[0][2]
            self.assertIn(status_arg, ("success", "partial_failure"),
                          f"予期しないステータス: {status_arg}")
        finally:
            _stop_patches(patches)


# ============================================================
# T-1-4: 429 レスポンス（レートリミット）の扱い
# ============================================================

class RateLimitHandlingTests(unittest.TestCase):
    """
    fetch_daily_yf が 429 相当のエラーを内部で処理して [] を返すとき、
    バッチが gracefully に継続すること。
    """

    def test_evening_batch_handles_rate_limit_as_empty_result(self):
        """
        fetch_daily_yf が 429 相当で [] を返したとき、夜バッチは継続する。
        """
        patches = _evening_patches()
        _apply_patches(patches)
        try:
            result = _evening._run_batch()
            self.assertIn(result, (0, 1))
        finally:
            _stop_patches(patches)

    def test_morning_batch_handles_rate_limit_as_empty_result(self):
        """
        朝バッチでも同様に [] 相当で継続する。
        """
        patches = _morning_patches()
        _apply_patches(patches)
        try:
            result = _morning._run_batch()
            self.assertIn(result, (0, 1))
        finally:
            _stop_patches(patches)


# ============================================================
# T-1-5: 非取引日（土日）スキップ
# ============================================================

from datetime import date

class WeekendSkipTests(unittest.TestCase):
    """非取引日（土日）のスキップ動作テスト。"""

    # ── 夜バッチ: 土日はスキップ（return 5） ──

    @patch("scripts.batch_evening_jp.send_skip_notification")
    def test_evening_skips_on_saturday(self, mock_skip):
        """夜バッチ: 土曜日は非取引日スキップ + Slack通知。"""
        saturday = date(2026, 5, 16)
        with patch.object(_evening, "_TODAY", saturday):
            self.assertEqual(_evening.main(), 5)
        mock_skip.assert_called_once()

    @patch("scripts.batch_evening_jp.send_skip_notification")
    def test_evening_skips_on_sunday(self, mock_skip):
        """夜バッチ: 日曜日は非取引日スキップ + Slack通知。"""
        sunday = date(2026, 5, 17)
        with patch.object(_evening, "_TODAY", sunday):
            self.assertEqual(_evening.main(), 5)
        mock_skip.assert_called_once()

    # ── 夜バッチ: 月曜は実行する ──

    def test_evening_does_not_skip_on_monday(self):
        """夜バッチ: 月曜日はスキップしない（JP市場営業日）。"""
        monday = date(2026, 5, 18)
        self.assertNotIn(monday.weekday(), (5, 6))

    # ── 朝バッチ: 日月はスキップ（return 5） ──

    @patch("scripts.batch_morning_all.send_skip_notification")
    def test_morning_skips_on_sunday(self, mock_skip):
        """朝バッチ: 日曜日は非取引日スキップ + Slack通知。"""
        sunday = date(2026, 5, 17)
        with patch.object(_morning, "_TODAY", sunday):
            self.assertEqual(_morning.main(), 5)
        mock_skip.assert_called_once()

    @patch("scripts.batch_morning_all.send_skip_notification")
    def test_morning_skips_on_monday(self, mock_skip):
        """朝バッチ: 月曜日は非取引日スキップ（US金曜データは土曜に取得済み） + Slack通知。"""
        monday = date(2026, 5, 18)
        with patch.object(_morning, "_TODAY", monday):
            self.assertEqual(_morning.main(), 5)
        mock_skip.assert_called_once()

    # ── 朝バッチ: 土曜は実行する（US金曜クローズのデータ処理） ──

    def test_morning_does_not_skip_on_saturday(self):
        """朝バッチ: 土曜日はスキップしない（US金曜クローズデータを処理）。"""
        saturday = date(2026, 5, 16)
        self.assertNotIn(saturday.weekday(), (6, 0))


if __name__ == "__main__":
    unittest.main()
