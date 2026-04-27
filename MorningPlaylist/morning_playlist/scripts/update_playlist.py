"""
Morning English プレイリスト更新スクリプト（エントリポイント）

毎朝タスクスケジューラから実行される。
  1. 各ポッドキャスト番組の最新エピソードを取得
  2. プレイリストの中身を差し替え
  3. Spotify Connect で再生開始
  4. Slack に結果を通知
"""

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

# .env 読み込み
load_dotenv(project_root / ".env")

import spotipy
from spotipy.oauth2 import SpotifyOAuth

from shared.config import init_config
from src.playlist_updater import run

SCOPES = (
    "playlist-read-private "
    "playlist-modify-public "
    "playlist-modify-private "
    "user-read-playback-state "
    "user-modify-playback-state"
)


def main():
    # 設定読み込み
    init_config(project_root / "config.json")

    # Spotify 認証
    sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
        client_id=os.getenv("SPOTIFY_CLIENT_ID"),
        client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
        redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"),
        scope=SCOPES,
        cache_path=str(project_root / ".cache"),
    ))

    run(sp)


if __name__ == "__main__":
    main()
