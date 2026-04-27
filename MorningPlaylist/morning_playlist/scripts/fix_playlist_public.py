"""プレイリストを公開に変更するワンショットスクリプト"""

from pathlib import Path
from dotenv import load_dotenv
import os

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(env_path)

import spotipy
from spotipy.oauth2 import SpotifyOAuth

sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    client_id=os.getenv("SPOTIFY_CLIENT_ID"),
    client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
    redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"),
    scope="playlist-read-private playlist-modify-public playlist-modify-private",
    cache_path=str(Path(__file__).resolve().parent.parent / ".cache"),
))

PLAYLIST_ID = "2v9RhPdEY0dcbZGIr6JjLS"

# 公開に変更
sp.playlist_change_details(PLAYLIST_ID, public=True)
print(f"OK: プレイリストを公開に変更しました")

# 確認
pl = sp.playlist(PLAYLIST_ID)
print(f"  名前: {pl['name']}")
print(f"  公開: {pl['public']}")
print(f"  曲数: {pl['tracks']['total']}")
