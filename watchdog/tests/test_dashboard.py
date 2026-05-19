"""
Watchdog Dashboard API テスト
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# リポジトリルートを sys.path に追加
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# shared.config を初期化してからアプリを import
from shared.config import init_config

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
init_config(_CONFIG_PATH)

from dashboard import create_app


@pytest.fixture()
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------
def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.data == b"OK"


# ---------------------------------------------------------------------------
# / (HTML)
# ---------------------------------------------------------------------------
def test_index_returns_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Watchdog" in resp.data
    assert b"dashboard.js" in resp.data


# ---------------------------------------------------------------------------
# /api/status — 正常系
# ---------------------------------------------------------------------------
_MOCK_RESULT_OK = {
    "label": "TestProject",
    "ok": True,
    "batches": [
        {"batch_type": "morning", "status": "success", "detail": "status=success, 終了=2025-01-01 08:00", "ok": True}
    ],
    "fatal_errors": {"count": 0, "ok": True, "samples": []},
    "log_file": {"exists": True, "ok": True, "path": "/tmp/batch_20250101.log"},
    "db_available": True,
}

_MOCK_RESULT_NG = {
    "label": "FailedProject",
    "ok": False,
    "batches": [
        {"batch_type": "evening_jp", "status": "failed", "detail": "status=failed", "ok": False}
    ],
    "fatal_errors": {"count": 2, "ok": False, "samples": ["2025-01-01 [module] Error: boom"]},
    "log_file": {"exists": False, "ok": False, "path": "/tmp/batch_20250101.log"},
    "db_available": True,
}


_MOCK_SCHED_ALL_OK = {
    "MorningEnglishPlaylistUpdate": {"state": "Ready", "last_run": "2026-05-19 07:00", "last_result": 0, "ok": True},
    "Ishikawa238_AutoCommit": {"state": "Ready", "last_run": "2026-05-19 03:00", "last_result": 0, "ok": True},
    "Ishikawa238_Watchdog": {"state": "Ready", "last_run": "2026-05-18 22:00", "last_result": 0, "ok": True},
    "Ishikawa238_WatchdogDashboard": {"state": "Running", "last_run": "2026-05-19 08:00", "last_result": 267009, "ok": True},
}


@patch("dashboard._get_scheduler_status", return_value=_MOCK_SCHED_ALL_OK)
@patch("dashboard.discover_new_projects", return_value=[])
@patch("dashboard.check_project", return_value=_MOCK_RESULT_OK)
def test_api_status_all_ok(mock_check, mock_discover, mock_sched, client):
    resp = client.get("/api/status")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data["all_ok"] is True
    assert "checked_at" in data
    assert len(data["projects"]) > 0
    # DB 監視プロジェクト
    db_projects = [p for p in data["projects"] if not p.get("scheduler_only")]
    assert len(db_projects) > 0
    proj = db_projects[0]
    assert proj["label"] == "TestProject"
    assert proj["ok"] is True
    assert proj["discovered"] is False


@patch("dashboard._get_scheduler_status", return_value=_MOCK_SCHED_ALL_OK)
@patch("dashboard.discover_new_projects", return_value=[])
@patch("dashboard.check_project", return_value=_MOCK_RESULT_NG)
def test_api_status_with_failure(mock_check, mock_discover, mock_sched, client):
    resp = client.get("/api/status")
    data = json.loads(resp.data)
    assert data["all_ok"] is False
    db_projects = [p for p in data["projects"] if not p.get("scheduler_only")]
    proj = db_projects[0]
    assert proj["ok"] is False
    assert proj["fatal_errors"]["count"] == 2


# ---------------------------------------------------------------------------
# /api/status — 自動発見あり
# ---------------------------------------------------------------------------
@patch("dashboard._get_scheduler_status", return_value=_MOCK_SCHED_ALL_OK)
@patch(
    "dashboard.discover_new_projects",
    return_value=[{"key": "new_sys", "cfg": {"label": "NewSystem"}}],
)
@patch("dashboard.check_project")
def test_api_status_with_discovery(mock_check, mock_discover, mock_sched, client):
    mock_check.return_value = _MOCK_RESULT_OK
    resp = client.get("/api/status")
    data = json.loads(resp.data)
    # 登録済み + 自動発見の両方が含まれる
    discovered_projects = [p for p in data["projects"] if p.get("discovered")]
    assert len(discovered_projects) >= 1


# ---------------------------------------------------------------------------
# /api/status — DB 接続失敗
# ---------------------------------------------------------------------------
@patch("dashboard._get_scheduler_status", return_value=_MOCK_SCHED_ALL_OK)
@patch("dashboard.discover_new_projects", return_value=[])
@patch(
    "dashboard.check_project",
    return_value={
        "label": "BrokenDB",
        "ok": False,
        "batches": [],
        "fatal_errors": {"count": 0, "ok": True, "samples": []},
        "log_file": {"exists": False, "ok": False, "path": ""},
        "db_available": False,
    },
)
def test_api_status_db_unavailable(mock_check, mock_discover, mock_sched, client):
    resp = client.get("/api/status")
    data = json.loads(resp.data)
    assert data["all_ok"] is False
    db_projects = [p for p in data["projects"] if not p.get("scheduler_only")]
    assert db_projects[0]["db_available"] is False


# ---------------------------------------------------------------------------
# /api/status — scheduler-only システム
# ---------------------------------------------------------------------------
_MOCK_SCHEDULER_STATUS = {
    "Ishikawa238_AutoCommit": {
        "state": "Ready",
        "last_run": "2026-05-19 03:00:12",
        "last_result": 0,
        "ok": True,
    },
    "MorningEnglishPlaylistUpdate": {
        "state": "Ready",
        "last_run": "2026-05-19 07:00:05",
        "last_result": 0,
        "ok": True,
    },
    "Ishikawa238_Watchdog": {
        "state": "Ready",
        "last_run": "2026-05-18 22:00:03",
        "last_result": 0,
        "ok": True,
    },
    "Ishikawa238_WatchdogDashboard": {
        "state": "Running",
        "last_run": "2026-05-19 08:00:00",
        "last_result": 267009,
        "ok": True,
    },
}


@patch("dashboard._get_scheduler_status", return_value=_MOCK_SCHEDULER_STATUS)
@patch("dashboard.discover_new_projects", return_value=[])
@patch("dashboard.check_project", return_value=_MOCK_RESULT_OK)
def test_api_status_includes_scheduler_only(mock_check, mock_discover, mock_sched, client):
    resp = client.get("/api/status")
    data = json.loads(resp.data)
    scheduler_projects = [p for p in data["projects"] if p.get("scheduler_only")]
    assert len(scheduler_projects) == 3  # MorningPlaylist, AutoCommit, Watchdog
    for proj in scheduler_projects:
        assert "scheduler_tasks_status" in proj
        assert len(proj["scheduler_tasks_status"]) > 0
        for task in proj["scheduler_tasks_status"]:
            assert "name" in task
            assert "state" in task
            assert "ok" in task


@patch("dashboard._get_scheduler_status", return_value={
    "Ishikawa238_AutoCommit": {
        "state": "NotRegistered", "last_run": None, "last_result": -1, "ok": False,
    },
})
@patch("dashboard.discover_new_projects", return_value=[])
@patch("dashboard.check_project", return_value=_MOCK_RESULT_OK)
def test_api_status_scheduler_task_not_registered(mock_check, mock_discover, mock_sched, client):
    resp = client.get("/api/status")
    data = json.loads(resp.data)
    assert data["all_ok"] is False  # scheduler-only NG → all_ok is False


# ---------------------------------------------------------------------------
# watchdog.check_batch — single_batch フラグ
# ---------------------------------------------------------------------------
def test_check_batch_single_batch():
    """single_batch=True で batch_type カラムなしテーブルに対応できることを確認。"""
    import sqlite3
    from watchdog import check_batch

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE batch_run_log (
            id INTEGER PRIMARY KEY,
            run_date TEXT,
            start_time TEXT,
            end_time TEXT,
            status TEXT
        )
    """)
    conn.execute("""
        INSERT INTO batch_run_log (run_date, start_time, end_time, status)
        VALUES ('2026-05-19', datetime('now', 'localtime'), datetime('now', 'localtime'), 'success')
    """)
    conn.commit()

    result = check_batch(conn, "morning", "daily", 26, single_batch=True)
    assert result["ok"] is True
    assert result["status"] == "success"
    assert result["batch_type"] == "morning"
    conn.close()


def test_check_batch_single_batch_missing():
    """single_batch=True でレコードなしの場合 missing を返す。"""
    import sqlite3
    from watchdog import check_batch

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE batch_run_log (
            id INTEGER PRIMARY KEY,
            run_date TEXT,
            start_time TEXT,
            end_time TEXT,
            status TEXT
        )
    """)
    conn.commit()

    result = check_batch(conn, "morning", "daily", 26, single_batch=True)
    assert result["ok"] is False
    assert result["status"] == "missing"
    conn.close()
