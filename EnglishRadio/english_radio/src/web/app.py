"""
EnglishRadio Web 管理画面（Flask）。

このPCからだけ使う前提（127.0.0.1 で待ち受け）。
他サイトからの勝手な操作を防ぐため、
  - Host ヘッダが localhost / 127.0.0.1 以外なら拒否（DNS リバインディング対策）
  - 変更系 API は JSON のみ受け付ける（クロスサイトのフォーム送信を拒否）
"""

import threading
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

from shared.config import get_config
from shared.db import get_connection
from shared.logger import get_logger
from src import config_store, feeds, paths, player, scheduler_info, status, store
from src.radio import AlreadyRunningError, RunOptions, ensure_db, run
from src.schedules import DAYS_OPTIONS, duration_minutes, validate_schedules

HISTORY_PAGE_SIZE = 20
REPLAY_M3U = "replay.m3u"
_ALLOWED_HOSTS = {"127.0.0.1", "localhost"}

_run_lock = threading.Lock()


def _logger():
    return get_logger(__name__)


# ---------------------------------------------------------------------------
# 表示用フィルタ
# ---------------------------------------------------------------------------

def fmt_minutes(seconds) -> str:
    if not seconds:
        return "—"
    minutes = round(seconds / 60)
    if minutes >= 60:
        return f"{minutes // 60}時間{minutes % 60}分"
    return f"{minutes}分"


def fmt_size(num) -> str:
    num = num or 0
    if num >= 1_073_741_824:
        return f"{num / 1_073_741_824:.1f}GB"
    return f"{num / 1_048_576:.0f}MB"


def fmt_dt(text) -> str:
    """'2026-11-19 07:00:03' → '11/19 07:00'"""
    if not text:
        return "—"
    text = str(text)
    return f"{text[5:7]}/{text[8:10]} {text[11:16]}".strip()


# ---------------------------------------------------------------------------
# 再生ヘルパー
# ---------------------------------------------------------------------------

def _play_entries(entries: list[dict], sources: list[str], label: str) -> dict:
    """エントリを VLC で再生し、履歴に残す。"""
    playable = [e for e in entries if Path(e["path"]).is_file() and paths.is_inside(Path(e["path"]), paths.library_dir())]
    if not playable:
        raise ValueError("再生できるファイルが見つかりません")
    m3u = player.write_m3u(playable, paths.data_dir() / REPLAY_M3U)
    player.play(m3u, get_config().get("radio", {}).get("vlc_path"))
    queue = list(zip(playable, sources))
    with get_connection(paths.db_path()) as conn:
        store.save_history(conn, queue, schedule_label=label, target_minutes=0, trigger="web")
        store.mark_played(conn, [e["guid"] for e in playable])
    return {"ok": True, "message": f"{len(playable)}本の再生を始めました ▶"}


def _run_in_background() -> None:
    try:
        run(RunOptions(play=True, trigger="web"))
    except Exception as e:  # 失敗は run() 内でログ・Slack 済み
        _logger().warning(f"Web からの実行が失敗しました: {e}")
    finally:
        _run_lock.release()


def _is_running() -> bool:
    if _run_lock.locked():
        return True
    with get_connection(paths.db_path()) as conn:
        return store.is_batch_running(conn)


def _error(message: str, code: int = 400):
    return jsonify({"ok": False, "message": message}), code


# ---------------------------------------------------------------------------
# アプリ本体
# ---------------------------------------------------------------------------

def create_app(config_path: Path) -> Flask:
    app = Flask(__name__)
    app.config["RADIO_CONFIG_PATH"] = Path(config_path)
    app.jinja_env.filters.update(minutes=fmt_minutes, size=fmt_size, dt=fmt_dt)
    ensure_db()

    @app.before_request
    def _guard():
        host = (request.host or "").split(":")[0]
        if host not in _ALLOWED_HOSTS:
            abort(403)
        if request.method in ("POST", "PUT", "DELETE") and not request.is_json:
            abort(415)

    @app.context_processor
    def _inject():
        return {"nav": request.endpoint, "days_options": DAYS_OPTIONS}

    _register_pages(app)
    _register_api(app)
    return app


def _register_pages(app: Flask) -> None:
    @app.route("/")
    def dashboard():
        return render_template("dashboard.html", d=status.dashboard())

    @app.route("/today")
    def today():
        with get_connection(paths.db_path()) as conn:
            latest = store.get_latest_history(conn)
        return render_template("today.html", h=latest, running=_is_running())

    @app.route("/history")
    def history():
        page = max(1, request.args.get("page", 1, type=int))
        with get_connection(paths.db_path()) as conn:
            rows = store.list_history(conn, HISTORY_PAGE_SIZE, (page - 1) * HISTORY_PAGE_SIZE)
            total = store.count_history(conn)
        pages = max(1, -(-total // HISTORY_PAGE_SIZE))
        return render_template("history.html", rows=rows, page=page, pages=pages)

    @app.route("/history/<int:history_id>")
    def history_detail(history_id: int):
        with get_connection(paths.db_path()) as conn:
            h = store.get_history(conn, history_id)
        if not h:
            return redirect(url_for("history"))
        return render_template("history_detail.html", h=h)

    @app.route("/library")
    def library_page():
        show = request.args.get("show") or None
        query = (request.args.get("q") or "").strip()[:100] or None
        with get_connection(paths.db_path()) as conn:
            episodes = store.list_episodes(conn, show, query)
            stats = store.library_stats(conn)
        return render_template("library.html", episodes=episodes, stats=stats, show=show, q=query or "")

    @app.route("/feeds")
    def feeds_page():
        return render_template("feeds.html", radio=get_config().get("radio", {}))

    @app.route("/schedule")
    def schedule_page():
        return render_template(
            "schedule.html",
            schedules=get_config().get("radio", {}).get("schedules", []),
            task=status.task_status(scheduler_info.MORNING_TASK),
        )


def _register_api(app: Flask) -> None:
    def cfg_path() -> Path:
        return app.config["RADIO_CONFIG_PATH"]

    @app.route("/health")
    def health():
        return jsonify({"ok": True})

    @app.route("/api/status")
    def api_status():
        with get_connection(paths.db_path()) as conn:
            batch = store.today_batch(conn)
        return jsonify({"running": _is_running(), "today": status.today_state(batch)["text"]})

    @app.route("/api/run", methods=["POST"])
    def api_run():
        if _is_running() or not _run_lock.acquire(blocking=False):
            return _error("すでに準備中です。少し待ってから再読み込みしてください", 409)
        threading.Thread(target=_run_in_background, daemon=True).start()
        return jsonify({"ok": True, "message": "新着を取りに行っています。1〜5分ほどで再生が始まります 📻"})

    @app.route("/api/play/today", methods=["POST"])
    def api_play_today():
        with get_connection(paths.db_path()) as conn:
            h = store.get_latest_history(conn)
        return _play_history(h)

    @app.route("/api/play/history/<int:history_id>", methods=["POST"])
    def api_play_history(history_id: int):
        with get_connection(paths.db_path()) as conn:
            h = store.get_history(conn, history_id)
        return _play_history(h)

    def _play_history(h: dict | None):
        if not h or not h["items"]:
            return _error("再生できるリストがありません", 404)
        try:
            label = f"{fmt_dt(h['played_at'])} のリストを再生"
            return jsonify(_play_entries(h["items"], [i["source"] for i in h["items"]], label))
        except (ValueError, player.VlcNotFoundError) as e:
            return _error(str(e))

    @app.route("/api/play/episode", methods=["POST"])
    def api_play_episode():
        guid = str((request.get_json(silent=True) or {}).get("guid", ""))
        with get_connection(paths.db_path()) as conn:
            ep = store.get_episode(conn, guid)
        if not ep:
            return _error("エピソードが見つかりません", 404)
        try:
            return jsonify(_play_entries([ep], ["shuffle"], f"1話再生: {ep['title'][:30]}"))
        except (ValueError, player.VlcNotFoundError) as e:
            return _error(str(e))

    @app.route("/api/feeds", methods=["GET", "PUT"])
    def api_feeds():
        if request.method == "GET":
            radio = get_config().get("radio", {})
            return jsonify({"feeds": radio.get("feeds", []), "episodes_per_feed": radio.get("episodes_per_feed", 3)})
        body = request.get_json(silent=True) or {}
        try:
            cleaned = config_store.validate_feeds(body.get("feeds"))
            per_feed = config_store.validate_episodes_per_feed(body.get("episodes_per_feed", 3))
        except ValueError as e:
            return _error(str(e))
        config_store.update_radio(cfg_path(), {"feeds": cleaned, "episodes_per_feed": per_feed})
        return jsonify({"ok": True, "message": "番組リストを保存しました ✅ 次回の再生から反映されます"})

    @app.route("/api/feeds/preview", methods=["POST"])
    def api_feeds_preview():
        url = str((request.get_json(silent=True) or {}).get("url", "")).strip()
        try:
            title, episodes = feeds.parse_feed(feeds.download_feed(url), "", 3)
        except Exception as e:
            return _error(f"RSS を読み込めませんでした: {e}")
        if not episodes:
            return _error("音声エピソードが見つかりませんでした（ポッドキャストの RSS か確認してください）")
        return jsonify({
            "ok": True,
            "title": title,
            "episodes": [{"title": e.title, "published": e.published, "minutes": fmt_minutes(e.duration_sec)} for e in episodes],
        })

    @app.route("/api/schedules", methods=["GET", "PUT"])
    def api_schedules():
        if request.method == "GET":
            return jsonify({"schedules": get_config().get("radio", {}).get("schedules", [])})
        try:
            cleaned = validate_schedules((request.get_json(silent=True) or {}).get("schedules"))
        except ValueError as e:
            return _error(str(e))
        config_store.update_radio(
            cfg_path(), {"schedules": cleaned, "target_minutes": duration_minutes(cleaned[0])}
        )
        ok, output = scheduler_info.reregister()
        status.clear_task_cache()
        if ok:
            return jsonify({"ok": True, "message": "保存して、タスクスケジューラにも反映しました ✅"})
        _logger().warning(f"タスク再登録に失敗: {output}")
        return jsonify({
            "ok": False,
            "message": "設定は保存しましたが、タスクスケジューラへの反映に失敗しました。"
                       "管理者 PowerShell で setup_scheduler.ps1 を実行してください",
            "detail": output,
        })
