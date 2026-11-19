"""
Watchdog Dashboard — 全システム稼働状況 Web ダッシュボード

各プロジェクトの batch_run_log / error_log を横断的に読み取り、
ブラウザで一覧表示する Flask アプリケーション。

DB を持たないシステム（MorningPlaylist, AutoCommit, Watchdog 自身）は
Windows タスクスケジューラの状態を取得して表示する。

データ取得ロジックは watchdog.py の既存関数をそのまま再利用する。
"""

import json
import subprocess
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, jsonify, render_template

from shared.config import get_config
from shared.logger import get_logger
from watchdog import check_project, discover_new_projects

logger = get_logger(__name__)

# JST (UTC+9)
_JST = timezone(timedelta(hours=9))

# ---------------------------------------------------------------------------
# システムメタデータ — ダッシュボードに表示する説明・スケジュール情報
# ---------------------------------------------------------------------------
_SYSTEM_META: dict[str, dict] = {
    "investment": {
        "description": "日米株式の投資判断支援。テクニカル指標算出、売買シグナル検知、保有銘柄監視 → Slack通知",
        "icon": "line-chart",
        "url": "http://127.0.0.1:5100/report",
        "tasks": ["Investment_Dashboard", "Investment_MorningBatch_ALL",
                  "Investment_EveningBatch_JP", "Investment_MonthlyBatch"],
        "batches": {
            "evening_jp":  {"schedule": "毎日 18:30", "label": "夕方バッチ (JP)"},
            "morning_all": {"schedule": "毎日 07:30", "label": "朝バッチ (US+JP)"},
            "monthly":     {"schedule": "毎月1日 08:00", "label": "月次バッチ"},
        },
    },
    "freelance_scout": {
        "description": "フリーランス案件自動収集。CrowdWorks・Coconala からスクレイプ → キーワードフィルタ → Slack通知",
        "icon": "briefcase",
        "tasks": ["FreelanceScout_MorningBatch"],
        "batches": {
            "morning": {"schedule": "毎日 07:00", "label": "朝バッチ"},
        },
    },
    "ai_news_digest": {
        "description": "AI/ML/LLM 関連ニュース自動収集。複数 RSS フィードから取得 → 重複排除 → Slack通知",
        "icon": "newspaper",
        "tasks": ["AiNewsDigest_MorningBatch"],
        "batches": {
            "morning": {"schedule": "毎日 07:00", "label": "朝バッチ"},
        },
    },
    "paper_watch": {
        "description": "arXiv 新着論文監視。指定カテゴリの論文を取得 → キーワードスコアリング → Slack通知",
        "icon": "book-open",
        "tasks": ["PaperWatch_MorningBatch"],
        "batches": {
            "morning": {"schedule": "毎日 07:00", "label": "朝バッチ"},
        },
    },
    "english_daily": {
        "description": "英語学習習慣化。曜日別カリキュラムを毎朝 Slack 配信 + 連続学習日数（ストリーク）管理",
        "icon": "languages",
        "tasks": ["EnglishDaily_MorningBatch", "EnglishDaily_Reminder_1730",
                  "EnglishDaily_Reminder_1930", "EnglishDaily_Reminder_2130",
                  "EnglishDaily_Reminder_2330"],
        "batches": {
            "morning": {"schedule": "毎日 07:00", "label": "朝バッチ"},
        },
    },
    "english_radio": {
        "description": "毎朝の英語ラジオ。ポッドキャスト RSS から mp3 を取得・蓄積 → 新着優先＋シャッフルで VLC 再生 → Slack通知",
        "icon": "radio",
        "url": "http://127.0.0.1:5210/",
        "tasks": ["EnglishRadio_MorningPlay", "EnglishRadio_WebServer", "EnglishRadio_StudyBatch"],
        "batches": {
            "morning": {"schedule": "毎日 07:00", "label": "朝の再生"},
            "study": {"schedule": "毎日 09:00", "label": "文字起こし・和訳"},
        },
    },
    "morning_playlist": {
        "description": "【停止中・EnglishRadio に移行】英語ポッドキャスト自動再生（Spotify API。認証切れのため停止）",
        "icon": "headphones",
        "scheduler_only": True,
        "tasks": ["MorningEnglishPlaylistUpdate"],
        "batches": {},
    },
    "auto_commit": {
        "description": "毎日深夜にリポジトリの変更を検知し、自動で git commit & push を実行",
        "icon": "git-commit",
        "scheduler_only": True,
        "tasks": ["Ishikawa238_AutoCommit"],
        "batches": {},
    },
    "watchdog": {
        "description": "全システム横断監視。バッチ未実行・致命的エラーを検知して Slack 通知",
        "icon": "shield-check",
        "scheduler_only": True,
        "tasks": ["Ishikawa238_Watchdog", "Ishikawa238_WatchdogDashboard"],
        "batches": {},
    },
}


# ---------------------------------------------------------------------------
# タスクスケジューラ状態取得
# ---------------------------------------------------------------------------

# PowerShell の LastTaskResult コード
_RESULT_OK = 0
_RESULT_RUNNING = 267009
_RESULT_NOT_YET_RUN = 267011   # タスク登録済みだが未実行
_RESULT_TERMINATED = 267014    # ユーザーにより終了


def _get_scheduler_status(task_names: list[str]) -> dict[str, dict]:
    """
    Windows タスクスケジューラからタスク状態を一括取得する。

    返り値: {"TaskName": {"state": str, "last_run": str|None,
                           "last_result": int, "ok": bool}}
    """
    if not task_names:
        return {}

    names_ps = ",".join(f"'{n}'" for n in task_names)
    ps_script = f"""
$names = @({names_ps})
$out = @()
foreach ($n in $names) {{
    $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
    if ($t) {{
        $info = $t | Get-ScheduledTaskInfo
        $lr = if ($info.LastRunTime -and $info.LastRunTime.Year -gt 1999) {{
            $info.LastRunTime.ToString("yyyy-MM-dd HH:mm:ss")
        }} else {{ $null }}
        $out += @{{ Name=$n; State=$t.State.ToString(); LastRun=$lr; Result=$info.LastTaskResult }}
    }} else {{
        $out += @{{ Name=$n; State="NotRegistered"; LastRun=$null; Result=-1 }}
    }}
}}
$out | ConvertTo-Json -Compress
"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            logger.warning("PowerShell task query failed: %s", proc.stderr.strip())
            return {}

        raw = proc.stdout.strip()
        if not raw:
            return {}

        data = json.loads(raw)
        # 単一タスクの場合 dict、複数の場合 list
        if isinstance(data, dict):
            data = [data]

        result = {}
        for item in data:
            name = item.get("Name", "")
            state = item.get("State", "NotRegistered")
            last_result = item.get("Result", -1)
            ok = last_result in (
                _RESULT_OK, _RESULT_RUNNING,
                _RESULT_NOT_YET_RUN, _RESULT_TERMINATED,
            )
            if state == "Running":
                ok = True
            result[name] = {
                "state": state,
                "last_run": item.get("LastRun"),
                "last_result": last_result,
                "ok": ok,
            }
        return result
    except Exception as e:
        logger.warning("Failed to query Task Scheduler: %s", e)
        return {}


def _build_scheduler_only_results() -> list[dict]:
    """scheduler_only なシステムのステータスを組み立てる。"""
    scheduler_only = {
        k: v for k, v in _SYSTEM_META.items() if v.get("scheduler_only")
    }
    if not scheduler_only:
        return []

    all_task_names = [
        t for meta in scheduler_only.values() for t in meta.get("tasks", [])
    ]
    scheduler_status = _get_scheduler_status(all_task_names)

    results = []
    for key, meta in scheduler_only.items():
        task_results = []
        for task_name in meta.get("tasks", []):
            ts = scheduler_status.get(task_name, {})
            task_results.append({
                "name": task_name,
                "state": ts.get("state", "NotRegistered"),
                "last_run": ts.get("last_run"),
                "last_result": ts.get("last_result", -1),
                "ok": ts.get("ok", False),
            })

        result = {
            "label": key.replace("_", " ").title().replace(" ", ""),
            "ok": all(t["ok"] for t in task_results) if task_results else False,
            "scheduler_only": True,
            "scheduler_tasks_status": task_results,
            "batches": [],
            "fatal_errors": {"count": 0, "ok": True, "samples": []},
            "log_file": {"ok": True},
            "db_available": False,
            "discovered": False,
        }
        _enrich_with_meta(result, key)
        results.append(result)

    return results


def _enrich_with_meta(result: dict, project_key: str) -> dict:
    """check_project() の結果にメタデータを付与する。"""
    key_lower = project_key.lower().replace("-", "_")
    meta = _SYSTEM_META.get(key_lower, {})

    result["description"] = meta.get("description", "")
    result["icon"] = meta.get("icon", "box")
    result["url"] = meta.get("url", "")
    result["scheduler_tasks"] = meta.get("tasks", [])

    # バッチごとのスケジュール・ラベルを付与
    batch_meta = meta.get("batches", {})
    for batch in result.get("batches", []):
        bm = batch_meta.get(batch["batch_type"], {})
        batch["schedule"] = bm.get("schedule", "")
        batch["label"] = bm.get("label", batch["batch_type"])

    return result


def create_app() -> Flask:
    """Flask アプリケーションファクトリ。"""
    app = Flask(__name__)

    @app.route("/")
    def index() -> str:
        return render_template("dashboard.html")

    @app.route("/api/status")
    def api_status() -> Response:
        """全プロジェクトの監視結果を JSON で返す。"""
        cfg = get_config()
        projects_cfg = cfg.get("projects", {})
        watchdog_cfg = cfg.get("watchdog", {})

        results: list[dict] = []

        # 登録済みプロジェクト（DB 監視）
        for key, proj_cfg in projects_cfg.items():
            result = check_project(key, proj_cfg, watchdog_cfg)
            result["discovered"] = False
            result["scheduler_only"] = False
            _enrich_with_meta(result, key)
            results.append(result)

        # 自動発見（DB 監視）
        discovered = discover_new_projects(set(projects_cfg.keys()), cfg)
        for item in discovered:
            result = check_project(item["key"], item["cfg"], watchdog_cfg)
            result["discovered"] = True
            result["scheduler_only"] = False
            _enrich_with_meta(result, item["key"])
            results.append(result)

        # scheduler-only システム（DB なし → タスクスケジューラ監視）
        results.extend(_build_scheduler_only_results())

        all_ok = all(r["ok"] for r in results)
        checked_at = datetime.now(tz=_JST).strftime("%Y-%m-%d %H:%M:%S")

        return jsonify({
            "all_ok": all_ok,
            "checked_at": checked_at,
            "projects": results,
        })

    @app.route("/health")
    def health() -> str:
        return "OK"

    return app
