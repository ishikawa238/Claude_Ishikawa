"""
朝バッチ（米国株含む全体）エントリーポイント

実行方法（investment_support/ から）:
    python -m scripts.batch_morning_all

実行タイミング: 毎日 07:30（Windowsタスクスケジューラで登録）

処理順序:
1. 米国株 + インデックス（SP500/NASDAQ）の価格データ取得
2. 米国株テクニカル指標計算
3. 市場環境モード確定（前夜バッチの JP + 今朝の US を統合）
4. 米国株: 流動性フィルタ → シグナル検出 → エントリーセット計算
5. 保有銘柄判定（JP+US 全体）
6. 朝通知（Slack）送信
7. バッチ実行ログ更新
"""

import atexit
import sys
import os
import signal
import threading
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PROJECT_ROOT.parent.parent  # Ishikawa238/ — shared パッケージの検索に必要
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))

from shared.batch import start_batch_log, finish_batch_log
from src.config import CONFIG
from src.data_fetcher import fetch_bulk_eod, fetch_index_history
from src.yfinance_fetcher import fetch_daily_yf, fetch_index_history_yf
from src.data_store import (
    fetch_all, fetch_one, get_connection,
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
from src.notifier import send_morning_report, send_skip_notification
from src.signal_detector import diagnose_signal_save_count, detect_and_save_signals
from src.technical_calculator import (
    calculate_and_save_index_mas, calculate_and_save_snapshots,
)

logger = get_logger(__name__)
_TODAY = date.today()
_BATCH_TYPE = "morning_all"
_LOCK_DIR = _PROJECT_ROOT / "data"
_LOCK_FILE = _LOCK_DIR / "batch_morning_all.lock"
_BATCH_TIMEOUT_SEC = 30 * 60  # 30分


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
    朝バッチ全体を実行する。
    返り値: 0=成功 / 1=部分失敗 / 2=致命的エラー / 3=二重起動スキップ / 4=タイムアウト / 5=非取引日スキップ
    """
    # 非取引日スキップ（日月: US市場は月〜金 ET。金曜データは土曜朝に取得済み）
    if _TODAY.weekday() in (6, 0):  # 6=Sunday, 0=Monday
        logger.info(f"[朝バッチ] 非取引日（{_TODAY}, {_TODAY.strftime('%A')}）のためスキップします")
        send_skip_notification(_TODAY, _BATCH_TYPE, "非取引日（週末）のため")
        return 5

    # 二重起動防止
    lock_fp = _acquire_lock()
    if lock_fp is None:
        logger.warning("[朝バッチ] 既に別プロセスが実行中のためスキップします")
        return 3

    # atexit でロック解放を保証（タイムアウト時の sys.exit でも実行される）
    atexit.register(_release_lock, lock_fp)

    # バッチ全体タイムアウト
    timeout_event = threading.Event()

    def _on_timeout():
        timeout_event.set()
        logger.error(f"[朝バッチ] タイムアウト（{_BATCH_TIMEOUT_SEC}秒）に到達しました")
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
        logger.warning("[朝バッチ] 既に別プロセスが実行中のためスキップします")
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
    """朝バッチの本体処理。"""
    _TOTAL = 7 if skip_llm else 8

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
        _progress(1, "US価格データ取得")
        logger.info("=" * 60)
        logger.info(f"[朝バッチ] 開始: {_TODAY}")
        logger.info("=" * 60)

        # ────────────────────────────────────────
        # STEP 1: 米国株 + US インデックス 価格取得
        # ────────────────────────────────────────
        for code in ("SP500", "NASDAQ"):
            rows = fetch_index_history(code, _TODAY - timedelta(days=5), _TODAY)
            if not rows:
                logger.warning(f"EODHD インデックス取得失敗（{code}）。yfinance にフォールバックします。")
                rows = fetch_index_history_yf(code, _TODAY - timedelta(days=5), _TODAY)
            if rows:
                with get_connection() as conn:
                    upsert(conn, "index_snapshot", rows)

        with get_connection() as conn:
            us_tickers = fetch_all(
                conn,
                "SELECT ticker FROM universe_master "
                "WHERE market='US' AND is_active=1 AND is_excluded=0",
            )
        tickers_with_market = [(r["ticker"], "US") for r in us_tickers]
        logger.info(f"対象銘柄: US {len(tickers_with_market)} 件")

        # 価格データ取得（EODHD Bulk: 1リクエストで US 全銘柄の最新取引日データを取得）
        # date を指定しない → EODHD が最新の取引日を自動選択する
        valid_tickers = {t for t, _ in tickers_with_market}
        all_rows = fetch_bulk_eod("US")
        if all_rows:
            all_rows = [r for r in all_rows if r["ticker"] in valid_tickers]
            logger.info(f"universe_master 照合後: {len(all_rows)} 件")
        if not all_rows:
            logger.warning("EODHD Bulk 取得失敗。yfinance にフォールバックします。")
            all_rows = fetch_daily_yf(tickers_with_market, _TODAY)
        failed_count = 0
        if not all_rows:
            logger.warning("US 価格取得失敗。price_history の更新をスキップします。")
            failed_count = len(tickers_with_market)
        else:
            _save_in_chunks(all_rows, "price_history")

        # US市場の実際の最新取引日を特定する。
        # 07:30 JST 時点ではUS市場の当日分がまだ存在しないため、
        # price_history の日付と technical_snapshot の日付を揃える必要がある。
        # 最も多くの銘柄がデータを持つ日付を採用する（テスト銘柄等に引きずられない）。
        if all_rows:
            from collections import Counter
            date_counts = Counter(r["date"] for r in all_rows)
            us_price_date = date.fromisoformat(max(date_counts, key=date_counts.get))
        else:
            us_price_date = _TODAY
        if us_price_date != _TODAY:
            logger.info(f"US 最新取引日: {us_price_date}（本日 {_TODAY} のデータなし）")

        # ────────────────────────────────────────
        # STEP 2: テクニカル指標計算（インデックスMA）
        # ────────────────────────────────────────
        _progress(2, "テクニカル指標計算")
        logger.info("STEP 2: テクニカル指標計算（US）")
        calculate_and_save_index_mas("SP500",  _TODAY)
        calculate_and_save_index_mas("NASDAQ", _TODAY)

        # ────────────────────────────────────────
        # STEP 3: 市場環境モード確定
        # ────────────────────────────────────────
        _progress(3, "市場環境モード確定")
        logger.info("STEP 3: 市場環境モード確定（US 確定 → 統合）")
        market_env = judge_market_environment(_TODAY, market="ALL")
        mode = market_env["mode"]

        # ────────────────────────────────────────
        # STEP 4: 米国株シグナル検出
        # ────────────────────────────────────────
        _progress(4, "USシグナル検出・エントリーセット計算")
        if failed_count == len(tickers_with_market):
            # 全銘柄の価格取得に失敗 → 古いデータでの誤シグナルを防ぐ
            logger.warning(
                "全銘柄の価格取得に失敗したため、テクニカル計算・シグナル検出をスキップします。"
            )
            screened = []
            us_entry_sets = []
            near_misses = []
            signals_count = 0
            screening_alert = None
        else:
            # テクニカル計算・スクリーニングは実際の取引日で実行する。
            # price_history.date と technical_snapshot.date が一致しないと
            # 流動性フィルタの JOIN が 0 件になるため。
            tickers_only = [t for t, _ in tickers_with_market]
            shares_map = load_shares_map(tickers_only)
            calculate_and_save_snapshots(
                tickers_with_market, us_price_date, shares_map=shares_map,
            )
            from src.rs_score import update_rs_scores
            update_rs_scores("US", us_price_date)

            logger.info("STEP 4: 流動性フィルタ・シグナル検出（US）")
            screened = get_screened_tickers(us_price_date, "US")
            screening_alert = (
                diagnose_screening_empty(us_price_date, "US") if not screened else None
            )
            if screening_alert:
                logger.warning(screening_alert.replace("\n", " | "))
                log_error(
                    _BATCH_TYPE, "get_screened_tickers",
                    "ScreeningEmpty", screening_alert[:500],
                )
            candidates = detect_and_save_signals(screened, us_price_date, mode)
            signals_count = len(candidates)
            signal_save_alert = diagnose_signal_save_count(
                us_price_date, "US", len(screened),
            )
            if signal_save_alert:
                logger.warning(signal_save_alert)
                log_error(
                    _BATCH_TYPE, "detect_and_save_signals",
                    "SignalSaveMismatch", signal_save_alert,
                )
            us_entry_sets = save_entry_sets(candidates, mode)

            from src.signal_detector import fetch_near_misses
            near_misses = fetch_near_misses(us_price_date, limit=5)

            # ペーパートレード: 仮想エントリー記録 + 日次更新
            from src.paper_trading import is_paper_mode_enabled, record_paper_entries, update_paper_trades
            if is_paper_mode_enabled():
                logger.info("STEP 4.5: ペーパートレード更新")
                record_paper_entries(us_entry_sets)
                update_paper_trades(_TODAY)

            try:
                from src.moomoo_watchlist import sync_watchlist
                sync_watchlist("US", us_price_date)
            except Exception as e:
                logger.warning(f"moomoo お気に入り同期スキップ: {e}")

        # 前夜バッチの JP entry_sets も取得（朝通知の上位2件に含める）
        jp_entry_sets = _load_last_evening_entry_sets()
        all_entry_sets = jp_entry_sets + us_entry_sets

        # ────────────────────────────────────────
        # STEP 5: 保有銘柄判定（JP+US 全体）
        # ────────────────────────────────────────
        _progress(5, "保有銘柄判定")
        logger.info("STEP 5: 保有銘柄判定")
        try:
            from src.moomoo_sync import sync_from_moomoo, sync_trade_results
            sync_from_moomoo()
            sync_trade_results()
        except Exception as e:
            logger.warning(f"moomoo 同期スキップ: {e}")
        sync_holdings_from_csv()
        ensure_holding_prices(_TODAY)  # ETF等 universe_master 未登録銘柄の価格を補完
        holdings = evaluate_holdings(_TODAY)

        # STEP 5.1: ポートフォリオスナップショット保存
        try:
            from src.portfolio_snapshot import compute_and_save_snapshot
            snapshot = compute_and_save_snapshot("morning_all", _TODAY)
            logger.info(f"ポートフォリオスナップショット保存完了: 総資産 ¥{snapshot['total_assets_combined_jpy']:,.0f}")
        except Exception as e:
            logger.warning(f"ポートフォリオスナップショット保存スキップ: {e}")

        # STEP 5.1: ポートフォリオスナップショット保存
        # ────────────────────────────────────────
        _progress(6, "ニュース取得")
        news = []
        try:
            from src.news_fetcher import fetch_market_news, fetch_ticker_news
            news = fetch_market_news(market="US", limit=5)
            tickers_names = [(es["ticker"], es.get("name", "")) for es in all_entry_sets]
            tickers_names += [(h["ticker"], h.get("name", "")) for h in holdings]
            if tickers_names:
                news += fetch_ticker_news(tickers_names, limit_per_ticker=1)
        except Exception as e:
            logger.warning(f"ニュース取得をスキップ: {e}")

        llm_feedback = []
        if not skip_llm:
            try:
                from src.llm_advisor import get_llm_feedback
                llm_feedback = get_llm_feedback(all_entry_sets, holdings, market_env, "morning", news=news)
            except Exception as e:
                logger.warning(f"LLMアドバイザーをスキップ: {e}")
        else:
            logger.info("STEP 5.5: LLMアドバイザーをスキップ（手動実行モード）")

        committee = None
        if not skip_llm:
            try:
                from src.committee import run_committee
                committee = run_committee(all_entry_sets, holdings, market_env, "morning", news=news)
            except Exception as e:
                logger.warning(f"投資委員会をスキップ: {e}")

        # ────────────────────────────────────────
        # STEP 6: 朝通知送信
        # ────────────────────────────────────────
        _progress(_TOTAL, "Slack通知送信")
        logger.info("STEP 6: 朝通知送信")
        data_insufficient = sum(1 for tk in screened if any(tk.get(f) is None for f in ("ema25", "ema75", "rsi14")))
        screening_stats = {
            "screened": len(screened),
            "candidates": signals_count,
            "entry_sets": len(us_entry_sets),
            "data_insufficient": data_insufficient,
            "alert": screening_alert,
        }
        try:
            send_morning_report(_TODAY, market_env, all_entry_sets, holdings,
                                llm_feedback=llm_feedback, near_misses=near_misses,
                                screening_stats=screening_stats, committee=committee)
        except Exception as e:
            logger.error(f"Slack通知送信失敗: {e}", exc_info=True)
            log_error(_BATCH_TYPE, "send_morning_report", type(e).__name__, str(e))

        # ────────────────────────────────────────
        # STEP 7: バッチログ更新
        # ────────────────────────────────────────
        status = (
            "partial_failure"
            if failed_count > 0 or screening_alert or signal_save_alert
            else "success"
        )
        import json as _json
        batch_memo = _json.dumps({
            "us_price_date": us_price_date.strftime("%Y-%m-%d"),
            "screening_alert": screening_alert,
            "signal_save_alert": signal_save_alert,
            "screened_count": len(screened),
        }, ensure_ascii=False)
        with get_connection() as conn:
            finish_batch_log(conn, batch_id, status,
                             {"total_tickers": len(tickers_with_market),
                              "failed_tickers": failed_count,
                              "signals_generated": signals_count,
                              "memo": batch_memo})
        logger.info(f"[朝バッチ] 完了: {status}")
        return 0 if status == "success" else 1

    except Exception as e:
        logger.error(f"[朝バッチ] 致命的エラー: {e}", exc_info=True)
        log_error(_BATCH_TYPE, "batch_morning_all", type(e).__name__, str(e), is_fatal=True)
        with get_connection() as conn:
            finish_batch_log(conn, batch_id, "failed",
                             {"total_tickers": 0, "failed_tickers": 0, "signals_generated": 0})
        return 2


# ============================================================
# 前夜バッチの JP エントリーセット取得
# ============================================================

def _load_last_evening_entry_sets() -> list[dict]:
    """
    前夜バッチで保存された JP の trade_signal_log を取得して
    朝通知用の形式で返す。daily_signals のスコアも結合する。
    """
    today_str = _TODAY.strftime("%Y-%m-%d")
    with get_connection() as conn:
        rows = fetch_all(
            conn,
            "SELECT t.*, "
            "  d.final_score, d.recommend_rank, d.recommend_reason "
            "FROM trade_signal_log t "
            "LEFT JOIN daily_signals d "
            "  ON t.ticker = d.ticker AND t.signal_date = d.date "
            "WHERE t.signal_date = ? AND t.market = 'JP' "
            "ORDER BY d.final_score DESC, t.id DESC LIMIT 3",
            (today_str,),
        )
    return [dict(r) for r in rows]


# ============================================================
# ユーティリティ（batch_evening_jp と同一）
# ============================================================

def _save_in_chunks(rows: list, table: str, chunk: int = 5000) -> None:
    for i in range(0, len(rows), chunk):
        with get_connection() as conn:
            upsert(conn, table, rows[i:i + chunk])




if __name__ == "__main__":
    sys.exit(main())
