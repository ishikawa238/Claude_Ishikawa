"""
夜バッチ（日本株）エントリーポイント

実行方法（investment_support/ から）:
    python -m scripts.batch_evening_jp

実行タイミング: 毎日 20:30（Windowsタスクスケジューラで登録）

処理順序:
1. 日本株 + インデックス（N225/TOPIX）の価格データ取得
2. テクニカル指標計算
3. 市場環境判定（日本株分）
4. 流動性フィルタ
5. パターンA/B シグナル検出
6. エントリーセット計算
7. 保有銘柄判定
8. 夜通知（Slack）送信
9. DBバックアップ
10. バッチ実行ログ更新
"""

import atexit
import sys
import os
import signal
from collections.abc import Callable
import threading
from datetime import date, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent  # Ishikawa238/ — shared パッケージの検索に必要
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

from shared.batch import start_batch_log, finish_batch_log
from src.config import CONFIG
from src.data_fetcher import fetch_index_history
from src.yfinance_fetcher import fetch_index_history_yf
from src.data_store import (
    backup_database, fetch_all, get_connection,
    log_error, upsert,
)
from src.entry_calculator import save_entry_sets
from src.holdings_monitor import ensure_holding_prices, evaluate_holdings, sync_holdings_from_csv
from src.logger import get_logger
from src.market_filter import (
    diagnose_screening_empty,
    get_screened_tickers,
    judge_market_environment,
)
from src.shares_outstanding import load_shares_map
from src.notifier import send_evening_report, send_skip_notification
from src.signal_detector import diagnose_signal_save_count, detect_and_save_signals
from src.technical_calculator import (
    calculate_and_save_index_mas, calculate_and_save_snapshots,
)
from src.trading_calendar import resolve_snapshot_date

logger = get_logger(__name__)
_TODAY = date.today()
_BATCH_TYPE = "evening_jp"
_LOCK_DIR = _PROJECT_ROOT / "data"
_LOCK_FILE = _LOCK_DIR / "batch_evening_jp.lock"
_BATCH_TIMEOUT_SEC = 30 * 60  # 30分


def _fetch_jp_daily_with_status(tickers_with_market: list[tuple[str, str]], target_date: date):
    """
    JP 日次価格を取得する。

    order_server 起動後に yfinance_fetcher が更新された場合、
    古い sys.modules キャッシュを避けるため都度 reload する。
    """
    import importlib
    import src.yfinance_fetcher as yf_mod

    importlib.reload(yf_mod)
    if hasattr(yf_mod, "fetch_daily_yf_with_status"):
        return yf_mod.fetch_daily_yf_with_status(tickers_with_market, target_date)

    # 旧版 yfinance_fetcher 向けフォールバック
    logger.warning(
        "fetch_daily_yf_with_status がありません。fetch_daily_yf にフォールバックします。"
    )
    from dataclasses import dataclass

    @dataclass
    class _PriceResult:
        rows: list
        total_tickers: int
        tickers_with_target_date: int
        failed_tickers: list
        source: str
        status: str

        @property
        def failed_count(self) -> int:
            return len(self.failed_tickers)

        @property
        def alert_message(self) -> str | None:
            if self.status == "failed" and self.total_tickers > 0:
                return (
                    "⚠️ *JP価格データ取得失敗*（ソース: yfinance）\n"
                    f"対象 {self.total_tickers} 銘柄のうち当日分を取得できませんでした。"
                )
            return None

    rows = yf_mod.fetch_daily_yf(tickers_with_market, target_date)
    total = len(tickers_with_market)
    target_str = target_date.strftime("%Y-%m-%d")
    on_date = len({r["ticker"] for r in rows if r.get("date") == target_str})
    status = "failed" if not rows else ("partial" if on_date < total * 0.5 else "ok")
    return _PriceResult(
        rows=rows,
        total_tickers=total,
        tickers_with_target_date=on_date,
        failed_tickers=[],
        source=getattr(yf_mod, "JP_PRICE_SOURCE", "yfinance"),
        status=status,
    )


def _acquire_lock() -> "open | None":
    """ファイルロックを取得する。取得できなければ None を返す。"""
    _LOCK_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fp = open(_LOCK_FILE, "w")
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fp.write(str(os.getpid()))
        fp.flush()
        return fp
    except (OSError, IOError):
        return None


def _release_lock(fp) -> None:
    """ファイルロックを解放する。"""
    if fp is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            fp.seek(0)
            msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fp, fcntl.LOCK_UN)
        fp.close()
    except (OSError, IOError):
        pass


def main() -> int:
    """
    夜バッチ全体を実行する。
    返り値: 0=成功 / 1=部分失敗 / 2=致命的エラー / 3=二重起動スキップ / 4=タイムアウト / 5=非取引日スキップ
    """
    # 非取引日スキップ（土日: TSE は月〜金のみ）
    if _TODAY.weekday() in (5, 6):  # 5=Saturday, 6=Sunday
        logger.info(f"[夜バッチ] 非取引日（{_TODAY}, {_TODAY.strftime('%A')}）のためスキップします")
        send_skip_notification(_TODAY, _BATCH_TYPE, "非取引日（週末）のため")
        return 5

    # 二重起動防止
    lock_fp = _acquire_lock()
    if lock_fp is None:
        logger.warning("[夜バッチ] 既に別プロセスが実行中のためスキップします")
        return 3

    # atexit でロック解放を保証（タイムアウト時の sys.exit でも実行される）
    atexit.register(_release_lock, lock_fp)

    # バッチ全体タイムアウト
    timeout_event = threading.Event()

    def _on_timeout():
        timeout_event.set()
        logger.error(f"[夜バッチ] タイムアウト（{_BATCH_TIMEOUT_SEC}秒）に到達しました")
        # os._exit ではなく sys.exit を使い、atexit ハンドラでロックを解放する
        sys.exit(4)

    timer = threading.Timer(_BATCH_TIMEOUT_SEC, _on_timeout)
    timer.daemon = True
    timer.start()

    try:
        return _run_batch()
    finally:
        timer.cancel()
        _release_lock(lock_fp)


def run_manual(
    skip_llm: bool = True,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> int:
    """
    手動実行用エントリーポイント。ロック取得・タイムアウト管理を含む。
    order_server から別スレッドで呼び出される。
    """
    lock_fp = _acquire_lock()
    if lock_fp is None:
        logger.warning("[夜バッチ] 既に別プロセスが実行中のためスキップします")
        return 3

    timer = threading.Timer(_BATCH_TIMEOUT_SEC, lambda: None)
    timer.daemon = True
    timer.start()
    try:
        return _run_batch(skip_llm=skip_llm, on_progress=on_progress)
    finally:
        timer.cancel()
        _release_lock(lock_fp)


def _run_batch(
    skip_llm: bool = False,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> int:
    """夜バッチの本体処理。"""
    _TOTAL = 8 if skip_llm else 9  # LLMスキップ時はニュース取得+通知で1ステップ減

    def _progress(step: int, label: str) -> None:
        if on_progress:
            on_progress(step, _TOTAL, label)

    with get_connection() as conn:
        batch_id = start_batch_log(conn, _BATCH_TYPE, _TODAY.strftime("%Y-%m-%d"),
                                   {"total_tickers": 0, "failed_tickers": 0, "signals_generated": 0})
    failed_count = 0
    signals_count = 0
    signal_save_alert = None

    try:
        # ────────────────────────────────────────
        # STEP 1: 日本株 + JP インデックス 価格取得
        # ────────────────────────────────────────
        _progress(1, "JP価格データ取得")
        logger.info("=" * 60)
        logger.info(f"[夜バッチ] 開始: {_TODAY}")
        logger.info("=" * 60)

        # JP インデックス取得
        for code in ("N225", "TOPIX"):
            rows = fetch_index_history(code, _TODAY - timedelta(days=5), _TODAY)
            if not rows:
                logger.warning(f"EODHD インデックス取得失敗（{code}）。yfinance にフォールバックします。")
                rows = fetch_index_history_yf(code, _TODAY - timedelta(days=5), _TODAY)
            if rows:
                with get_connection() as conn:
                    upsert(conn, "index_snapshot", rows)

        # JP 銘柄一覧取得（テクニカル計算用）
        with get_connection() as conn:
            jp_tickers = fetch_all(
                conn,
                "SELECT ticker FROM universe_master "
                "WHERE market='JP' AND is_active=1 AND is_excluded=0",
            )
        tickers_with_market = [(r["ticker"], "JP") for r in jp_tickers]
        logger.info(f"対象銘柄: JP {len(tickers_with_market)} 件")

        # 価格データ取得（yfinance: EODHD JP Bulk が利用不可のため代替）
        price_result = _fetch_jp_daily_with_status(tickers_with_market, _TODAY)
        all_rows = price_result.rows
        jp_price_alert = price_result.alert_message
        if price_result.status == "failed":
            logger.warning(
                "JP 価格取得失敗（yfinance）。price_history の更新をスキップします。"
            )
            failed_count = len(tickers_with_market)
        elif not all_rows:
            logger.warning("JP 価格取得失敗。price_history の更新をスキップします。")
            failed_count = len(tickers_with_market)
        else:
            _save_in_chunks(all_rows, "price_history")
            failed_count = price_result.failed_count

        # price_history.date と technical_snapshot.date を揃える（朝バッチ US と同様）
        jp_price_date = resolve_snapshot_date(
            _TODAY, "JP", all_rows if all_rows else None,
        )
        if jp_price_date and jp_price_date != _TODAY:
            logger.info(
                f"JP 最新取引日: {jp_price_date}（本日 {_TODAY} の価格行なし）"
            )

        # ────────────────────────────────────────
        # STEP 2: テクニカル指標計算（インデックスMA）
        # ────────────────────────────────────────
        _progress(2, "テクニカル指標計算")
        logger.info("STEP 2: テクニカル指標計算")
        index_date = jp_price_date or _TODAY
        calculate_and_save_index_mas("N225", index_date)
        calculate_and_save_index_mas("TOPIX", index_date)

        # ────────────────────────────────────────
        # STEP 3: 市場環境判定（JP のみ）
        # ────────────────────────────────────────
        _progress(3, "市場環境判定")
        logger.info("STEP 3: 市場環境判定")
        market_env = judge_market_environment(index_date, market="JP")
        mode = market_env["mode"]

        # ────────────────────────────────────────
        # STEP 4-6: スクリーニング → シグナル → エントリーセット
        # ────────────────────────────────────────
        _progress(4, "スクリーニング・シグナル検出")
        if jp_price_date is None:
            logger.warning(
                "JP 価格データが存在しないため、テクニカル計算・シグナル検出をスキップします。"
            )
            screened = []
            entry_sets = []
            near_misses = []
            signals_count = 0
            screening_alert = None
        else:
            tickers_only = [t for t, _ in tickers_with_market]
            shares_map = load_shares_map(tickers_only)
            calculate_and_save_snapshots(
                tickers_with_market, jp_price_date, shares_map=shares_map,
            )
            if _TODAY != jp_price_date:
                _prune_jp_technical_orphans(_TODAY)

            logger.info("STEP 4: 流動性フィルタ")
            screened = get_screened_tickers(jp_price_date, "JP")
            screening_alert = (
                diagnose_screening_empty(jp_price_date, "JP") if not screened else None
            )
            if screening_alert:
                logger.warning(screening_alert.replace("\n", " | "))
                log_error(
                    _BATCH_TYPE, "get_screened_tickers",
                    "ScreeningEmpty", screening_alert[:500],
                )

            logger.info("STEP 5: シグナル検出")
            candidates = detect_and_save_signals(screened, jp_price_date, mode)
            signals_count = len(candidates)
            signal_save_alert = diagnose_signal_save_count(
                jp_price_date, "JP", len(screened),
            )
            if signal_save_alert:
                logger.warning(signal_save_alert)
                log_error(
                    _BATCH_TYPE, "detect_and_save_signals",
                    "SignalSaveMismatch", signal_save_alert,
                )

            _progress(5, "エントリーセット計算")
            logger.info("STEP 6: エントリーセット計算")
            entry_sets = save_entry_sets(candidates, mode)

            from src.signal_detector import fetch_near_misses
            near_misses = fetch_near_misses(jp_price_date, market="JP", limit=5)

            # ペーパートレード: 仮想エントリー記録 + 日次更新
            from src.paper_trading import is_paper_mode_enabled, record_paper_entries, update_paper_trades
            if is_paper_mode_enabled():
                logger.info("STEP 6.5: ペーパートレード更新")
                record_paper_entries(entry_sets)
                update_paper_trades(jp_price_date)

        # ────────────────────────────────────────
        # STEP 7: 保有銘柄判定
        # ────────────────────────────────────────
        _progress(6, "保有銘柄判定")
        logger.info("STEP 7: 保有銘柄判定")
        try:
            from src.moomoo_sync import sync_from_moomoo, sync_trade_results
            sync_from_moomoo()
            sync_trade_results()
        except Exception as e:
            logger.warning(f"moomoo 同期スキップ: {e}")
        sync_holdings_from_csv()
        eval_date = jp_price_date or _TODAY
        ensure_holding_prices(eval_date)  # ETF等 universe_master 未登録銘柄の価格を補完
        _update_earnings_schedule_for_holdings()
        holdings = evaluate_holdings(eval_date)

        # STEP 7.1: ポートフォリオスナップショット保存
        try:
            from src.portfolio_snapshot import compute_and_save_snapshot
            snapshot = compute_and_save_snapshot("evening_jp", _TODAY)
            logger.info(f"ポートフォリオスナップショット保存完了: 総資産 ¥{snapshot['total_assets_combined_jpy']:,.0f}")
        except Exception as e:
            logger.warning(f"ポートフォリオスナップショット保存スキップ: {e}")

        # STEP 7.1: ポートフォリオスナップショット保存
        # ────────────────────────────────────────
        _progress(7, "ニュース取得")
        news = []
        try:
            from src.news_fetcher import fetch_market_news, fetch_ticker_news
            news = fetch_market_news(market="JP", limit=5)
            # 買い候補 + 保有銘柄の銘柄別ニュースも追加
            tickers_names = [(es["ticker"], es.get("name", "")) for es in entry_sets]
            tickers_names += [(h["ticker"], h.get("name", "")) for h in holdings]
            if tickers_names:
                news += fetch_ticker_news(tickers_names, limit_per_ticker=1)
        except Exception as e:
            logger.warning(f"ニュース取得をスキップ: {e}")

        llm_feedback = []
        if not skip_llm:
            try:
                from src.llm_advisor import get_llm_feedback
                llm_feedback = get_llm_feedback(entry_sets, holdings, market_env, "evening", news=news)
            except Exception as e:
                logger.warning(f"LLMアドバイザーをスキップ: {e}")
        else:
            logger.info("STEP 7.5: LLMアドバイザーをスキップ（手動実行モード）")

        # ────────────────────────────────────────
        # STEP 8: 夜通知送信
        # ────────────────────────────────────────
        _progress(_TOTAL, "Slack通知送信")
        logger.info("STEP 8: 夜通知送信")
        data_insufficient = sum(1 for tk in screened if any(tk.get(f) is None for f in ("ema25", "ema75", "rsi14")))
        screening_stats = {
            "screened": len(screened),
            "candidates": signals_count,
            "entry_sets": len(entry_sets),
            "data_insufficient": data_insufficient,
            "alert": screening_alert,
        }
        try:
            send_evening_report(_TODAY, market_env, entry_sets, holdings, failed_count,
                                llm_feedback=llm_feedback, near_misses=near_misses,
                                screening_stats=screening_stats,
                                price_source_alert=jp_price_alert)
        except Exception as e:
            logger.error(f"Slack通知送信失敗: {e}", exc_info=True)
            log_error(_BATCH_TYPE, "send_evening_report", type(e).__name__, str(e))

        # ────────────────────────────────────────
        # STEP 9: DBバックアップ
        # ────────────────────────────────────────
        logger.info("STEP 9: DBバックアップ")
        backup_database()

        # ────────────────────────────────────────
        # STEP 10: バッチログ更新
        # ────────────────────────────────────────
        status = (
            "partial_failure"
            if failed_count > 0 or screening_alert or signal_save_alert
            else "success"
        )
        import json as _json
        batch_memo = _json.dumps({
            "jp_price_source": price_result.source,
            "jp_price_status": price_result.status,
            "jp_price_target_date_count": price_result.tickers_with_target_date,
            "jp_price_date": jp_price_date.isoformat() if jp_price_date else None,
            "screening_alert": screening_alert,
            "signal_save_alert": signal_save_alert,
            "screened_count": len(screened),
        }, ensure_ascii=False)
        with get_connection() as conn:
            finish_batch_log(conn, batch_id, status,
                             {"total_tickers": len(tickers_with_market), "failed_tickers": failed_count,
                              "signals_generated": signals_count, "memo": batch_memo})
        logger.info(f"[夜バッチ] 完了: {status}")
        return 0 if status == "success" else 1

    except Exception as e:
        logger.error(f"[夜バッチ] 致命的エラー: {e}", exc_info=True)
        log_error(_BATCH_TYPE, "batch_evening_jp", type(e).__name__, str(e), is_fatal=True)
        with get_connection() as conn:
            finish_batch_log(conn, batch_id, "failed",
                             {"total_tickers": 0, "failed_tickers": 0, "signals_generated": 0})
        return 2


# ============================================================
# ユーティリティ
# ============================================================

def _update_earnings_schedule_for_holdings() -> None:
    """保有中の JP 銘柄（最大3件程度）の決算予定日を J-Quants から更新する。"""
    from src.jquants_fetcher import fetch_and_save_earnings_schedule
    with get_connection() as conn:
        rows = fetch_all(
            conn,
            "SELECT DISTINCT h.ticker FROM holdings h "
            "INNER JOIN universe_master u ON h.ticker = u.ticker "
            "WHERE h.is_closed=0 AND u.market='JP'",
        )
    jp_tickers = [r["ticker"] for r in rows]
    if jp_tickers:
        fetch_and_save_earnings_schedule(jp_tickers, _TODAY)


def _prune_jp_technical_orphans(calendar_date: date) -> None:
    """価格行のない暦日にだけ残った JP technical_snapshot を削除する。"""
    orphan = calendar_date.strftime("%Y-%m-%d")
    with get_connection() as conn:
        cur = conn.execute(
            """DELETE FROM technical_snapshot
               WHERE date = ?
                 AND ticker IN (
                   SELECT ticker FROM universe_master WHERE market = 'JP'
                 )""",
            (orphan,),
        )
        if cur.rowcount:
            logger.info(f"日付不一致の JP technical_snapshot を {cur.rowcount} 件削除 ({orphan})")


def _save_in_chunks(rows: list, table: str, chunk: int = 5000) -> None:
    for i in range(0, len(rows), chunk):
        with get_connection() as conn:
            upsert(conn, table, rows[i:i + chunk])




if __name__ == "__main__":
    sys.exit(main())
