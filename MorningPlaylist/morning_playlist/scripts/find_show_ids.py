"""番組の show_id を検索して確定するスクリプト"""

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
    scope="playlist-read-private",
    cache_path=str(Path(__file__).resolve().parent.parent / ".cache"),
))

queries = [
    "NPR Up First",
    "BBC Global News Podcast",
    "TED Radio Hour",
    "6 Minute English BBC",
    "All Ears English",
]

for q in queries:
    result = sp.search(q=q, type="show", market="JP", limit=3)
    shows = result.get("shows", {}).get("items", [])
    print(f"\n--- {q} ---")
    for s in shows:
        pub = s.get("publisher", "不明")
        eps = s.get("total_episodes", "?")
        print(f"  {s['name']}  |  配信: {pub}  |  EP数: {eps}  |  ID: {s['id']}")
