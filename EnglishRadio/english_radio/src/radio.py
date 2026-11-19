"""
朝の英語ラジオ: 全体の流れ。

RSS 取得 → 新着ダウンロード → 選曲（新着優先＋シャッフル） → m3u → VLC 再生
→ 履歴保存 → Slack 通知。batch_run_log に実行結果を残す（watchdog が監視する）。
"""

from dataclasses import dataclass, field
from datetime import datetime

from shared.batch import finish_batch_log, mark_stale_running_batches, start_batch_log
from shared.config import get_config
from shared.db import get_connection
from shared.logger import get_logger
from src import feeds, library, notifier, paths, player, store
from src.schedules import active_schedule, duration_minutes
from src.selector import build_queue

BATCH_TYPE = "morning"
TODAY_M3U = "today.m3u"


class AlreadyRunningError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunOptions:
    play: bool = True
    dry_run: bool = False
    trigger: str = "schedule"  # schedule / manual / web


@dataclass
class RunResult:
    label: str
    target_minutes: int
    new_count: int = 0
    queue: list = field(default_factory=list)
    history_id: int | None = None


def _logger():
    return get_logger(__name__)


def ensure_db() -> None:
    paths.db_path().parent.mkdir(parents=True, exist_ok=True)
    with get_connection(paths.db_path()) as conn:
        store.init_db(conn)


def resolve_target(radio_cfg: dict, now: datetime | None = None) -> tuple[str, int]:
    """今回のラベルと目標分数を決める。スケジュール外なら target_minutes を使う。"""
    sched = active_schedule(radio_cfg.get("schedules", []), now)
    if sched:
        return sched.get("label", sched["start"]), duration_minutes(sched)
    return "英語ラジオ", int(radio_cfg.get("target_minutes", 90))


def _start_log() -> int:
    with get_connection(paths.db_path()) as conn:
        mark_stale_running_batches(conn, BATCH_TYPE)
        if store.is_batch_running(conn, BATCH_TYPE):
            raise AlreadyRunningError("すでに実行中です。終わるまでお待ちください")
        return start_batch_log(conn, BATCH_TYPE, datetime.now().strftime("%Y-%m-%d"))


def _collect(radio_cfg: dict, dry_run: bool) -> tuple[list[dict], list[dict]]:
    """(新着エントリ, ライブラリ全体) を返す。dry_run ではダウンロードしない。"""
    limit = int(radio_cfg.get("episodes_per_feed", 3))
    episodes = feeds.fetch_all(radio_cfg.get("feeds", []), limit)
    with get_connection(paths.db_path()) as conn:
        if dry_run:
            unknown = [e for e in episodes if e.guid not in store.known_guids(conn)]
            _logger().info(f"[dry-run] 未取得 {len(unknown)} 件（ダウンロードはしません）")
            new_entries = []
        else:
            new_entries = library.download_new(conn, episodes, paths.library_dir())
    with get_connection(paths.db_path()) as conn:
        return new_entries, store.list_episodes(conn)


def _play_and_record(queue, result: RunResult, options: RunOptions, vlc_path: str | None) -> None:
    m3u = player.write_m3u([e for e, _ in queue], paths.data_dir() / TODAY_M3U)
    if options.dry_run:
        return
    if options.play:
        used = player.play(m3u, vlc_path)
        _logger().info(f"再生開始: {used}")
    with get_connection(paths.db_path()) as conn:
        result.history_id = store.save_history(
            conn, queue,
            schedule_label=result.label,
            target_minutes=result.target_minutes,
            trigger=options.trigger,
            status="played" if options.play else "queued",
        )
        if options.play:
            store.mark_played(conn, [e["guid"] for e, _ in queue])


def _execute(options: RunOptions, result: RunResult) -> None:
    radio_cfg = get_config().get("radio", {})
    new_entries, all_entries = _collect(radio_cfg, options.dry_run)
    result.new_count = len(new_entries)
    result.queue = build_queue(new_entries, all_entries, result.target_minutes)
    if not result.queue:
        raise RuntimeError("再生できるエピソードがありません（ライブラリが空です）")
    total_min = sum(e["duration_sec"] for e, _ in result.queue) / 60
    _logger().info(f"選択: {len(result.queue)} 本 / {total_min:.0f}分（新着 {result.new_count} 本取得）")
    _play_and_record(result.queue, result, options, radio_cfg.get("vlc_path"))


def run(options: RunOptions = RunOptions()) -> RunResult:
    """メイン処理。失敗時は Slack に通知して例外を再送出する。"""
    ensure_db()
    label, target = resolve_target(get_config().get("radio", {}))
    _logger().info(f"開始: '{label}' 目標 {target}分 (trigger={options.trigger})")
    result = RunResult(label=label, target_minutes=target)
    # dry-run は試し実行なので、watchdog が見る実行ログには残さない
    batch_id = None if options.dry_run else _start_log()
    try:
        _execute(options, result)
    except Exception as e:
        _logger().error(f"処理失敗: {e}")
        if batch_id is not None:
            with get_connection(paths.db_path()) as conn:
                finish_batch_log(conn, batch_id, "failed", {"message": str(e)[:500]})
                store.log_error(conn, __name__, e, fatal=True)
        notifier.send_failure(e, dry_run=options.dry_run)
        raise

    with get_connection(paths.db_path()) as conn:
        if batch_id is not None:
            finish_batch_log(conn, batch_id, "success", {
                "new_episodes": result.new_count,
                "queued_episodes": len(result.queue),
            })
        stats = store.library_stats(conn)
    if options.play or options.dry_run:
        notifier.send_success(result.queue, stats, label, dry_run=options.dry_run)
    return result
