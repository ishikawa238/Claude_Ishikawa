"""
学習バッチ: 文字起こし → 和訳・要約・単語抽出。

朝の再生とは別のプロセス（タスク EnglishRadio_StudyBatch）か、Web の「文字起こし」ボタンから動く。
1話の失敗は記録して次へ進む（失敗した話は次回やり直す。文字起こしは最大3回まで）。
batch_run_log には batch_type="study" で残す（watchdog が監視）。
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from shared.batch import finish_batch_log, mark_stale_running_batches, start_batch_log
from shared.config import get_config
from shared.db import get_connection
from shared.logger import get_logger
from src import paths, store
from src.radio import AlreadyRunningError, ensure_db
from src.study import ai, transcribe
from src.study import store as study_store

BATCH_TYPE = "study"
DEFAULT_MAX_AUDIO_MINUTES = 120


@dataclass(frozen=True)
class StudyOptions:
    guid: str | None = None  # 指定すると、その1話だけ処理する
    dry_run: bool = False


@dataclass
class StudyResult:
    transcribed: int = 0
    studied: int = 0
    failed: int = 0
    ai_skipped: bool = False
    planned: list = field(default_factory=list)

    def message(self) -> str:
        text = f"文字起こし {self.transcribed}本 / 和訳・単語 {self.studied}本"
        if self.failed:
            text += f" / 失敗 {self.failed}件"
        if self.ai_skipped:
            text += "（API キー未設定のため和訳はお休み）"
        return text


def _logger():
    return get_logger(__name__)


def _study_cfg() -> dict:
    return get_config().get("study", {})


def _start_log() -> int:
    with get_connection(paths.db_path()) as conn:
        mark_stale_running_batches(conn, BATCH_TYPE)
        if store.is_batch_running(conn, BATCH_TYPE):
            raise AlreadyRunningError("文字起こしはすでに実行中です。終わるまでお待ちください")
        return start_batch_log(conn, BATCH_TYPE, datetime.now().strftime("%Y-%m-%d"))


def pick_within_budget(episodes: list[dict], max_minutes: int) -> list[dict]:
    """合計の長さが max_minutes に収まる分だけ選ぶ（1本目は長くても必ず選ぶ）。"""
    picked: list[dict] = []
    total = 0
    for ep in episodes:
        minutes = (ep.get("duration_sec") or 0) / 60
        if picked and total + minutes > max_minutes:
            continue
        picked.append(ep)
        total += minutes
    return picked


def _transcribe_targets(guid: str | None, max_minutes: int) -> list[dict]:
    with get_connection(paths.db_path()) as conn:
        if guid:
            ep = store.get_episode(conn, guid)
            if not ep:
                raise ValueError("エピソードが見つかりません")
            study_store.reset_attempts(conn, guid)
            return [ep] if guid not in study_store.transcribed_guids(conn) else []
        return pick_within_budget(study_store.transcript_targets(conn), max_minutes)


def _transcribe_one(ep: dict, model: str) -> bool:
    path = Path(ep["path"])
    try:
        if not path.is_file() or not paths.is_inside(path, paths.library_dir()):
            raise FileNotFoundError(f"mp3 が見つかりません: {path.name}")
        started = datetime.now()
        segments = transcribe.transcribe(path, model)
    except ImportError:
        raise  # faster-whisper 未インストールは全体の失敗にする（1話ずつ失敗回数を使わない）
    except Exception as e:
        _logger().warning(f"文字起こし失敗: {ep['title'][:40]}: {e}")
        with get_connection(paths.db_path()) as conn:
            study_store.mark_transcript_failed(conn, ep["guid"], str(e))
        return False
    with get_connection(paths.db_path()) as conn:
        study_store.save_transcript(conn, ep["guid"], segments, model)
    took = (datetime.now() - started).total_seconds() / 60
    _logger().info(f"文字起こし完了: {ep['title'][:40]}（{len(segments)}文 / {took:.1f}分）")
    return True


def _study_one(client, settings: ai.AISettings, ep: dict) -> bool:
    with get_connection(paths.db_path()) as conn:
        segments = study_store.get_segments(conn, ep["guid"])
    try:
        translations = ai.translate(client, settings.model, segments)
        summary, vocab = ai.extract_study(client, settings.model, segments, ep.get("title", ""))
    except ai.StudyAIError as e:
        _logger().warning(f"和訳・単語抽出に失敗: {ep['title'][:40]}: {e}")
        with get_connection(paths.db_path()) as conn:
            study_store.set_ai_status(conn, ep["guid"], "failed", str(e))
        return False
    with get_connection(paths.db_path()) as conn:
        study_store.save_translations(conn, ep["guid"], translations)
        added = study_store.save_study(conn, ep["guid"], summary, vocab)
    _logger().info(f"和訳・単語抽出完了: {ep['title'][:40]}（単語 {added}語）")
    return True


def _ai_phase(guid: str | None, result: StudyResult) -> None:
    with get_connection(paths.db_path()) as conn:
        targets = [e for e in study_store.ai_targets(conn) if not guid or e["guid"] == guid]
    if not targets:
        return
    settings = ai.settings_from_config(_study_cfg())
    if settings is None:
        result.ai_skipped = True
        with get_connection(paths.db_path()) as conn:
            for ep in targets:
                study_store.set_ai_status(conn, ep["guid"], "skipped")
        _logger().info("Anthropic API キーが未設定のため、和訳と単語抽出はお休みします")
        return
    client = ai.make_client(settings)
    for ep in targets:
        if _study_one(client, settings, ep):
            result.studied += 1
        else:
            result.failed += 1


def _execute(options: StudyOptions, result: StudyResult) -> None:
    cfg = _study_cfg()
    max_minutes = int(cfg.get("max_audio_minutes", DEFAULT_MAX_AUDIO_MINUTES))
    targets = _transcribe_targets(options.guid, max_minutes)
    result.planned = [e["guid"] for e in targets]
    if options.dry_run:
        for ep in targets:
            _logger().info(f"[dry-run] 文字起こし予定: {ep['show']} / {ep['title'][:50]}")
        return
    model = cfg.get("whisper_model") or transcribe.DEFAULT_MODEL
    for ep in targets:
        if _transcribe_one(ep, model):
            result.transcribed += 1
        else:
            result.failed += 1
    _ai_phase(options.guid, result)


def run(options: StudyOptions = StudyOptions()) -> StudyResult:
    """学習バッチ本体。全体が失敗したときは例外を再送出する。"""
    ensure_db()
    result = StudyResult()
    if not _study_cfg().get("enabled", True) and not options.guid:
        _logger().info("学習機能はお休み中です（study.enabled = false）")
        return result
    batch_id = None if options.dry_run else _start_log()
    try:
        _execute(options, result)
    except Exception as e:
        _logger().error(f"学習バッチ失敗: {e}")
        if batch_id is not None:
            with get_connection(paths.db_path()) as conn:
                finish_batch_log(conn, batch_id, "failed", {"message": str(e)[:500]})
                store.log_error(conn, __name__, e, fatal=True, batch_type=BATCH_TYPE)
        raise
    _logger().info(f"学習バッチ完了: {result.message()}")
    if batch_id is not None:
        status = "partial_failure" if result.failed and (result.transcribed or result.studied) else (
            "failed" if result.failed else "success")
        with get_connection(paths.db_path()) as conn:
            finish_batch_log(conn, batch_id, status, {
                "new_episodes": result.transcribed,
                "queued_episodes": result.studied,
                "message": result.message(),
            })
    return result
