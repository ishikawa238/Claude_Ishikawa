"""
Spotify Connect デバイス一覧と再生テスト

Echo や PC が Spotify Connect デバイスとして見えるか確認し、
プレイリストの再生を API から直接開始する。
"""

from pathlib import Path
from dotenv import load_dotenv
import os

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(env_path)

import spotipy
from spotipy.oauth2 import SpotifyOAuth

SCOPES = (
    "playlist-read-private playlist-modify-public playlist-modify-private "
    "user-read-playback-state user-modify-playback-state"
)

sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    client_id=os.getenv("SPOTIFY_CLIENT_ID"),
    client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
    redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"),
    scope=SCOPES,
    cache_path=str(Path(__file__).resolve().parent.parent / ".cache"),
))

PLAYLIST_ID = "2v9RhPdEY0dcbZGIr6JjLS"

# --- ステップ1: 利用可能なデバイスを一覧 ---
print("=" * 50)
print("[ステップ1] Spotify Connect デバイス一覧")
print("=" * 50)
devices = sp.devices()
if not devices["devices"]:
    print("  デバイスが見つかりません。")
    print("  → PC の Spotify アプリを起動するか、Echo で何か再生してください。")
    print("  → その後、このスクリプトを再実行してください。")
    exit(1)

for i, d in enumerate(devices["devices"]):
    active = " (アクティブ)" if d["is_active"] else ""
    print(f"  [{i}] {d['name']}  |  タイプ: {d['type']}  |  ID: {d['id']}{active}")

# --- ステップ2: デバイスを選んで再生 ---
print()
print("=" * 50)
print("[ステップ2] 再生テスト")
print("=" * 50)

choice = input(f"\n  再生するデバイスの番号を入力 [0-{len(devices['devices'])-1}]: ").strip()
try:
    idx = int(choice)
    device = devices["devices"][idx]
except (ValueError, IndexError):
    print("  無効な番号です。終了します。")
    exit(1)

print(f"  → {device['name']} で Morning English を再生します...")

try:
    sp.start_playback(
        device_id=device["id"],
        context_uri=f"spotify:playlist:{PLAYLIST_ID}",
    )
    print(f"  OK: 再生開始！")
except Exception as e:
    print(f"  NG: 再生失敗: {e}")
