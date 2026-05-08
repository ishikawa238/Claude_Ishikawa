"""
ポッドキャスト番組設定 Web UI

ブラウザで番組の追加・削除・保存を行う。
変更は config.json に保存され、次回のプレイリスト更新で反映される。

実行方法（morning_playlist/ から）:
    python scripts/podcast_selector.py

    ブラウザで http://127.0.0.1:5200/ を開く
"""

import json
import sys
from pathlib import Path

# プロジェクトルートを sys.path に追加
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

# リポジトリルート（shared パッケージ用）
repo_root = project_root.parent.parent
sys.path.insert(0, str(repo_root))

from dotenv import load_dotenv
import os

load_dotenv(project_root / ".env")

from shared.config import init_config

init_config(project_root / "config.json")

import spotipy
from spotipy.oauth2 import SpotifyOAuth
from flask import Flask, request, jsonify

from shared.logger import get_logger

logger = get_logger(__name__)

_CONFIG_PATH = project_root / "config.json"
_HOST = "127.0.0.1"
_PORT = 5200

SCOPES = (
    "playlist-read-private "
    "playlist-modify-public "
    "playlist-modify-private "
    "user-read-playback-state "
    "user-modify-playback-state"
)

_sp = None


def _get_spotify() -> spotipy.Spotify:
    """Spotify クライアントを遅延初期化して返す。"""
    global _sp
    if _sp is None:
        _sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
            client_id=os.getenv("SPOTIFY_CLIENT_ID"),
            client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
            redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"),
            scope=SCOPES,
            cache_path=str(project_root / ".cache"),
        ))
    return _sp


app = Flask(__name__)


# ============================================================
# config 読み書き
# ============================================================

def _load_config() -> dict:
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _load_shows() -> list[dict]:
    cfg = _load_config()
    return cfg.get("spotify", {}).get("shows", [])


def _save_shows(shows: list[dict]) -> None:
    cfg = _load_config()
    cfg["spotify"]["shows"] = shows
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    logger.info("config.json を更新: %d 番組", len(shows))


def _load_schedules() -> list[dict]:
    cfg = _load_config()
    return cfg.get("spotify", {}).get("schedules", [])


def _save_schedules(schedules: list[dict]) -> None:
    cfg = _load_config()
    cfg["spotify"]["schedules"] = schedules
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    logger.info("config.json を更新: %d スケジュール", len(schedules))


# ============================================================
# API ルート
# ============================================================

@app.route("/health")
def health():
    return "OK", 200


@app.route("/api/shows")
def api_get_shows():
    return jsonify(_load_shows())


@app.route("/api/shows", methods=["PUT"])
def api_put_shows():
    data = request.get_json()
    if not isinstance(data, list):
        return jsonify({"error": "配列が必要です"}), 400
    for item in data:
        if not isinstance(item, dict):
            return jsonify({"error": "各要素はオブジェクトである必要があります"}), 400
        if not item.get("name") or not item.get("id"):
            return jsonify({"error": "name と id は必須です"}), 400
    _save_shows(data)
    return jsonify({"status": "ok"})


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    result = _get_spotify().search(q=q, type="show", market="JP", limit=10)
    items = result.get("shows", {}).get("items", [])
    return jsonify([
        {
            "name": s["name"],
            "id": s["id"],
            "publisher": s.get("publisher", ""),
            "total_episodes": s.get("total_episodes", 0),
            "image_url": (s.get("images") or [{}])[0].get("url", ""),
        }
        for s in items
    ])


@app.route("/api/schedules")
def api_get_schedules():
    return jsonify(_load_schedules())


@app.route("/api/schedules", methods=["PUT"])
def api_put_schedules():
    data = request.get_json()
    if not isinstance(data, list):
        return jsonify({"error": "配列が必要です"}), 400
    for item in data:
        if not isinstance(item, dict):
            return jsonify({"error": "各要素はオブジェクトである必要があります"}), 400
        if not item.get("start") or not item.get("end"):
            return jsonify({"error": "start と end は必須です"}), 400
    _save_schedules(data)
    return jsonify({"status": "ok"})


# ============================================================
# メインページ HTML
# ============================================================

def _render_page() -> str:
    return """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Morning English - ポッドキャスト設定</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
<style>
  body { padding: 24px; font-size: 14px; background: #f8f9fa; }
  .card { border-radius: 12px; padding: 20px; background: #fff;
          box-shadow: 0 2px 8px rgba(0,0,0,0.08); margin-bottom: 24px; }
  .show-item { display: flex; align-items: center; padding: 10px 0;
               border-bottom: 1px solid #eee; }
  .show-item:last-child { border-bottom: none; }
  .show-info { flex: 1; }
  .show-name { font-weight: 600; }
  .show-meta { font-size: 0.8rem; color: #888; }
  .show-img { width: 48px; height: 48px; border-radius: 8px;
              object-fit: cover; margin-right: 12px; }
  .show-img-placeholder { width: 48px; height: 48px; border-radius: 8px;
                          background: #e9ecef; margin-right: 12px;
                          display: flex; align-items: center; justify-content: center;
                          color: #adb5bd; font-size: 1.2rem; }
  .status-msg { margin-top: 12px; font-size: 0.9rem; }
  .footer-note { text-align: center; color: #888; font-size: 0.85rem;
                 margin-top: 16px; }
  h1 { font-size: 1.3rem; font-weight: 700; margin-bottom: 4px; }
  h2 { font-size: 1.05rem; font-weight: 600; margin-bottom: 16px; }
  .subtitle { color: #888; font-size: 0.85rem; margin-bottom: 24px; }
  .badge-added { font-size: 0.75rem; }
  .sched-item { padding: 12px 0; border-bottom: 1px solid #eee; }
  .sched-item:last-child { border-bottom: none; }
  .sched-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .sched-label { flex: 1; min-width: 100px; }
  .sched-device { width: 120px; }
  .sched-time-group { display: flex; align-items: center; gap: 4px; }
  .sched-separator { color: #888; padding: 0 2px; }
  .sched-duration { font-size: 0.8rem; color: #888; margin-top: 4px; padding-left: 4px; }
  .main-layout { display: flex; gap: 24px; align-items: flex-start; }
  .col-left { flex: 1; min-width: 0; }
  .col-right { width: 340px; flex-shrink: 0; }
  @media (max-width: 768px) {
    .main-layout { flex-direction: column; }
    .col-right { width: 100%; }
  }
</style>
</head>
<body>
<div class="container" style="max-width: 960px;">

  <h1>Morning English</h1>
  <div class="subtitle">ポッドキャスト番組設定</div>

  <div class="main-layout">
    <div class="col-left">
      <!-- 登録済みの番組 -->
      <div class="card">
        <h2>登録済みの番組 <span id="show-count" class="badge bg-secondary">0</span></h2>
        <div id="show-list"></div>
        <div style="margin-top: 16px; display: flex; align-items: center; gap: 12px;">
          <button id="btn-save" class="btn btn-primary" disabled onclick="saveShows()">
            保存
          </button>
          <span id="status-msg" class="status-msg"></span>
        </div>
      </div>

      <!-- 番組を検索して追加 -->
      <div class="card">
        <h2>番組を検索して追加</h2>
        <div class="input-group mb-3">
          <input type="text" id="search-input" class="form-control"
                 placeholder="番組名を入力..." onkeydown="if(event.key==='Enter') searchShows()">
          <button class="btn btn-outline-secondary" onclick="searchShows()">検索</button>
        </div>
        <div id="search-results"></div>
      </div>
    </div>

    <div class="col-right">
      <!-- スケジュール設定 -->
      <div class="card">
        <h2>スケジュール設定 <span id="sched-count" class="badge bg-secondary">0</span></h2>
        <p class="show-meta" style="margin-bottom: 12px;">何時から何時まで再生するかを設定します</p>
        <div id="sched-list"></div>
        <button class="btn btn-outline-primary btn-sm" style="margin-top: 8px;" onclick="addSchedule()">
          + 新しいスケジュール
        </button>
        <div style="margin-top: 16px; display: flex; align-items: center; gap: 12px;">
          <button id="btn-save-sched" class="btn btn-primary" disabled onclick="saveSchedules()">
            保存
          </button>
          <span id="sched-status-msg" class="status-msg"></span>
        </div>
      </div>
    </div>
  </div>

  <div class="footer-note">
    変更は次回のプレイリスト更新から反映されます
  </div>
</div>

<script>
let shows = [];
let dirty = false;
let schedules = [];
let schedDirty = false;

// ---- 初期読み込み ----
async function loadShows() {
  const res = await fetch('/api/shows');
  shows = await res.json();
  dirty = false;
  renderShows();
}

// ---- 描画 ----
function renderShows() {
  const list = document.getElementById('show-list');
  const count = document.getElementById('show-count');
  const btn = document.getElementById('btn-save');
  count.textContent = shows.length;
  btn.disabled = !dirty;

  if (shows.length === 0) {
    list.innerHTML = '<div style="color:#888; padding:12px 0;">番組が登録されていません</div>';
    return;
  }
  list.innerHTML = shows.map((s, i) => `
    <div class="show-item">
      <div class="show-img-placeholder">&#9835;</div>
      <div class="show-info">
        <div class="show-name">${esc(s.name)}</div>
        <div class="show-meta">ID: ${esc(s.id)}</div>
      </div>
      <button class="btn btn-outline-danger btn-sm" onclick="removeShow(${i})">削除</button>
    </div>
  `).join('');

  // 検索結果の追加済み表示を更新
  renderSearchAddedState();
}

function removeShow(idx) {
  shows.splice(idx, 1);
  dirty = true;
  renderShows();
}

// ---- 保存 ----
async function saveShows() {
  const btn = document.getElementById('btn-save');
  const msg = document.getElementById('status-msg');
  btn.disabled = true;
  msg.textContent = '保存中...';
  msg.className = 'status-msg text-muted';

  try {
    const res = await fetch('/api/shows', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(shows),
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.error || '保存に失敗しました');
    }
    dirty = false;
    msg.textContent = '保存しました';
    msg.className = 'status-msg text-success';
    renderShows();
  } catch (e) {
    msg.textContent = e.message;
    msg.className = 'status-msg text-danger';
    btn.disabled = false;
  }
}

// ---- スケジュール ----
async function loadSchedules() {
  const res = await fetch('/api/schedules');
  schedules = await res.json();
  schedDirty = false;
  renderSchedules();
}

function renderSchedules() {
  const list = document.getElementById('sched-list');
  const count = document.getElementById('sched-count');
  const btn = document.getElementById('btn-save-sched');
  count.textContent = schedules.length;
  btn.disabled = !schedDirty;

  if (schedules.length === 0) {
    list.innerHTML = '<div style="color:#888; padding:12px 0;">スケジュールが登録されていません</div>';
    return;
  }
  list.innerHTML = schedules.map((s, i) => `
    <div class="sched-item">
      <div class="sched-row">
        <input type="text" class="form-control form-control-sm sched-label"
               value="${esc(s.label || '')}" placeholder="ラベル"
               onchange="updateSched(${i},'label',this.value)">
        <div class="sched-time-group">
          <input type="time" class="form-control form-control-sm"
                 value="${esc(s.start || '')}"
                 onchange="updateSched(${i},'start',this.value)">
          <span class="sched-separator">~</span>
          <input type="time" class="form-control form-control-sm"
                 value="${esc(s.end || '')}"
                 onchange="updateSched(${i},'end',this.value)">
        </div>
        <input type="text" class="form-control form-control-sm sched-device"
               value="${esc(s.device_name || '')}" placeholder="デバイス名"
               onchange="updateSched(${i},'device_name',this.value)">
        <button class="btn btn-outline-danger btn-sm" onclick="removeSched(${i})">削除</button>
      </div>
      <div class="sched-duration">${calcDuration(s.start, s.end)}</div>
    </div>
  `).join('');
}

function updateSched(idx, key, val) {
  schedules[idx][key] = val;
  schedDirty = true;
  renderSchedules();
}

function removeSched(idx) {
  schedules.splice(idx, 1);
  schedDirty = true;
  renderSchedules();
}

function addSchedule() {
  schedules.push({ label: '', start: '07:00', end: '08:30', device_name: 'Echo' });
  schedDirty = true;
  renderSchedules();
}

function calcDuration(start, end) {
  if (!start || !end) return '';
  const [sh, sm] = start.split(':').map(Number);
  const [eh, em] = end.split(':').map(Number);
  const mins = (eh * 60 + em) - (sh * 60 + sm);
  if (mins <= 0) return '* 無効な時間範囲';
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  if (h > 0 && m > 0) return h + '時間' + m + '分';
  if (h > 0) return h + '時間';
  return m + '分';
}

async function saveSchedules() {
  const btn = document.getElementById('btn-save-sched');
  const msg = document.getElementById('sched-status-msg');
  btn.disabled = true;
  msg.textContent = '保存中...';
  msg.className = 'status-msg text-muted';

  try {
    const res = await fetch('/api/schedules', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(schedules),
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.error || '保存に失敗しました');
    }
    schedDirty = false;
    msg.innerHTML = '保存しました<br><small class="text-muted">スケジューラに反映するには管理者権限 PowerShell で <code>.\\\\setup_scheduler.ps1</code> を再実行</small>';
    msg.className = 'status-msg text-success';
    renderSchedules();
  } catch (e) {
    msg.textContent = e.message;
    msg.className = 'status-msg text-danger';
    btn.disabled = false;
  }
}

// ---- 検索 ----
async function searchShows() {
  const input = document.getElementById('search-input');
  const q = input.value.trim();
  if (!q) return;

  const container = document.getElementById('search-results');
  container.innerHTML = '<div class="text-muted">検索中...</div>';

  try {
    const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
    if (!res.ok) throw new Error('検索に失敗しました');
    const results = await res.json();
    if (results.length === 0) {
      container.innerHTML = '<div class="text-muted">見つかりませんでした</div>';
      return;
    }
    container.innerHTML = results.map(r => {
      const added = shows.some(s => s.id === r.id);
      const imgHtml = r.image_url
        ? `<img class="show-img" src="${esc(r.image_url)}" alt="">`
        : `<div class="show-img-placeholder">&#9835;</div>`;
      return `
        <div class="show-item" data-id="${esc(r.id)}">
          ${imgHtml}
          <div class="show-info">
            <div class="show-name">${esc(r.name)}</div>
            <div class="show-meta">${esc(r.publisher)} / ${r.total_episodes} エピソード</div>
          </div>
          ${added
            ? '<span class="badge bg-secondary badge-added">追加済み</span>'
            : `<button class="btn btn-outline-success btn-sm"
                 onclick="addShow('${escAttr(r.name)}','${escAttr(r.id)}')">追加</button>`
          }
        </div>
      `;
    }).join('');
  } catch (e) {
    container.innerHTML = `<div class="text-danger">${esc(e.message)}</div>`;
  }
}

function addShow(name, id) {
  if (shows.some(s => s.id === id)) return;
  shows.push({name, id});
  dirty = true;
  renderShows();
}

function renderSearchAddedState() {
  document.querySelectorAll('#search-results .show-item').forEach(el => {
    const id = el.dataset.id;
    const added = shows.some(s => s.id === id);
    const actionEl = el.querySelector('.btn-outline-success, .badge-added');
    if (!actionEl) return;
    if (added && actionEl.classList.contains('btn-outline-success')) {
      const badge = document.createElement('span');
      badge.className = 'badge bg-secondary badge-added';
      badge.textContent = '追加済み';
      actionEl.replaceWith(badge);
    }
  });
}

// ---- ユーティリティ ----
function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
function escAttr(s) {
  return s.replace(/\\\\/g,'\\\\\\\\').replace(/'/g,"\\\\'");
}

// ---- 未保存の離脱防止 ----
window.addEventListener('beforeunload', e => {
  if (dirty || schedDirty) { e.preventDefault(); }
});

// ---- 起動 ----
loadShows();
loadSchedules();
</script>
</body>
</html>"""


@app.route("/")
def index():
    return _render_page()


# ============================================================
# エントリポイント
# ============================================================

if __name__ == "__main__":
    logger.info("ポッドキャスト設定サーバー起動: http://%s:%d/", _HOST, _PORT)
    app.run(host=_HOST, port=_PORT, debug=False)
