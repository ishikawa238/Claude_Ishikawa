"""
学習機能の Web ページと API。

  /study                 文字起こしの状況一覧（学習できる回 / 待ち / 失敗）
  /study/episode?guid=   学習ページ（ブラウザ再生 + 字幕同期 + 和訳 + 単語）
  /vocab                 単語帳（一覧・復習モード）
  /audio?guid=           学習ページ用の mp3 配信（library 配下だけ。Range 対応）
guid は URL を含むことがあるので、パスではなくクエリで受け取る。
"""

import threading
from datetime import date
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for

from shared.db import get_connection
from shared.logger import get_logger
from src import paths, store
from src.study import runner
from src.study import store as study_store

_study_lock = threading.Lock()


def _logger():
    return get_logger(__name__)


def _error(message: str, code: int = 400):
    return jsonify({"ok": False, "message": message}), code


def study_running() -> bool:
    if _study_lock.locked():
        return True
    with get_connection(paths.db_path()) as conn:
        return store.is_batch_running(conn, runner.BATCH_TYPE)


def _run_study(options: runner.StudyOptions) -> None:
    try:
        runner.run(options)
    except Exception as e:  # 失敗はログと batch_run_log に残っている
        _logger().warning(f"Web からの文字起こしが失敗しました: {e}")
    finally:
        _study_lock.release()


def _start_study(options: runner.StudyOptions):
    if study_running() or not _study_lock.acquire(blocking=False):
        return _error("文字起こしはいま実行中です。終わってからもう一度お試しください", 409)
    threading.Thread(target=_run_study, args=(options,), daemon=True).start()
    return None


def group_study_episodes(episodes: list[dict]) -> dict[str, list[dict]]:
    """学習できる回 / 待ち / 失敗 に分ける。"""
    groups: dict[str, list[dict]] = {"ready": [], "pending": [], "failed": []}
    for ep in episodes:
        if ep["t_status"] == "done":
            groups["ready"].append(ep)
        elif ep["t_status"] == "failed":
            groups["failed"].append(ep)
        else:
            groups["pending"].append(ep)
    return groups


def _guid_arg() -> str:
    return (request.args.get("guid") or "").strip()[:500]


def register_study(app: Flask) -> None:
    _register_pages(app)
    _register_api(app)


def _register_pages(app: Flask) -> None:
    @app.route("/study")
    def study_index():
        with get_connection(paths.db_path()) as conn:
            groups = group_study_episodes(study_store.list_study_episodes(conn))
            stats = study_store.study_stats(conn)
        return render_template("study_index.html", groups=groups, stats=stats, running=study_running())

    @app.route("/study/episode")
    def study_detail():
        guid = _guid_arg()
        with get_connection(paths.db_path()) as conn:
            ep = store.get_episode(conn, guid)
            transcript = study_store.get_transcript(conn, guid) if ep else None
        if not ep:
            return redirect(url_for("study_index"))
        return render_template("study.html", ep=ep, t=transcript, running=study_running())

    @app.route("/vocab")
    def vocab_page():
        show = request.args.get("show") or None
        status = request.args.get("status") if request.args.get("status") in ("new", "learning", "known") else None
        review = request.args.get("mode") == "review"
        with get_connection(paths.db_path()) as conn:
            words = study_store.list_vocab(conn, due_only=review, status=status, show=show)
            stats = study_store.study_stats(conn)
            shows = sorted({w["show"] for w in study_store.list_vocab(conn) if w["show"]})
        return render_template("vocab.html", words=words, stats=stats, review=review, show=show,
                               status=status, shows=shows, today=date.today().isoformat())


def _register_api(app: Flask) -> None:
    @app.route("/audio")
    def audio():
        with get_connection(paths.db_path()) as conn:
            ep = store.get_episode(conn, _guid_arg())
        if not ep:
            abort(404)
        path = Path(ep["path"])
        if not path.is_file() or not paths.is_inside(path, paths.library_dir()):
            abort(404)
        return send_file(path, mimetype="audio/mpeg", conditional=True, max_age=3600)

    @app.route("/api/study/transcribe", methods=["POST"])
    def api_study_transcribe():
        guid = str((request.get_json(silent=True) or {}).get("guid", ""))[:500]
        with get_connection(paths.db_path()) as conn:
            if not store.get_episode(conn, guid):
                return _error("エピソードが見つかりません", 404)
        busy = _start_study(runner.StudyOptions(guid=guid))
        if busy:
            return busy
        return jsonify({"ok": True, "message": "文字起こしを始めました 📝 6分の回で数分、50分の回は20分ほどかかります"})

    @app.route("/api/study/run", methods=["POST"])
    def api_study_run():
        busy = _start_study(runner.StudyOptions())
        if busy:
            return busy
        return jsonify({"ok": True, "message": "まだの回をまとめて文字起こしします 📝 終わるまで時間がかかります"})

    @app.route("/api/study/status")
    def api_study_status():
        guid = _guid_arg()
        with get_connection(paths.db_path()) as conn:
            t = study_store.get_transcript(conn, guid) if guid else None
        return jsonify({"running": study_running(), "status": t["status"] if t else None,
                        "ai_status": t["ai_status"] if t else None})

    @app.route("/api/vocab/<int:vocab_id>", methods=["PUT"])
    def api_vocab(vocab_id: int):
        body = request.get_json(silent=True) or {}
        with get_connection(paths.db_path()) as conn:
            if "known" in body:
                word = study_store.set_vocab_known(conn, vocab_id, bool(body["known"]))
            elif body.get("result") in ("ok", "ng"):
                word = study_store.review_vocab(conn, vocab_id, body["result"] == "ok")
            else:
                return _error("result（ok / ng）か known を指定してください")
        if not word:
            return _error("単語が見つかりません", 404)
        return jsonify({"ok": True, "word": word})

