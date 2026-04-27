"""
Spotify API 手動検証スクリプト

ステップ2: OAuth認証テスト
ステップ3: ポッドキャストエピソード取得 & プレイリスト更新テスト

使い方:
  1. .env に SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET を設定
  2. python scripts/verify_spotify.py
  3. 初回はブラウザが開くのでSpotifyにログインして認可する
"""

import sys
from pathlib import Path
from dotenv import load_dotenv
import os

# .env 読み込み
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(env_path)

import spotipy
from spotipy.oauth2 import SpotifyOAuth

# --- 設定 ---
CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")

SCOPES = "playlist-read-private playlist-modify-public playlist-modify-private"

# 検証対象のポッドキャスト (show_id)
# Spotify で番組ページを開き URL の /show/ の後の部分が show_id
TEST_SHOWS = {
    "NPR Up First": "2mTUnDkuKUkhiueKcVWoP0",
    # BBC と TED は日本リージョンで利用不可の可能性あり → 検索で確認
}

PLAYLIST_NAME = "Morning English"


def check_env():
    """ステップ0: 環境変数の確認"""
    print("=" * 50)
    print("[ステップ0] 環境変数の確認")
    print("=" * 50)
    ok = True
    if not CLIENT_ID:
        print("  NG: SPOTIFY_CLIENT_ID が未設定")
        ok = False
    else:
        print(f"  OK: SPOTIFY_CLIENT_ID = {CLIENT_ID[:8]}...")
    if not CLIENT_SECRET:
        print("  NG: SPOTIFY_CLIENT_SECRET が未設定")
        ok = False
    else:
        print(f"  OK: SPOTIFY_CLIENT_SECRET = {CLIENT_SECRET[:4]}...")
    print(f"  INFO: REDIRECT_URI = {REDIRECT_URI}")
    if not ok:
        print("\n.env ファイルに認証情報を設定してください。")
        sys.exit(1)
    print()


def test_auth():
    """ステップ2: OAuth認証テスト"""
    print("=" * 50)
    print("[ステップ2] OAuth 認証テスト")
    print("=" * 50)
    print("  ブラウザが開いたら Spotify にログインして認可してください...")
    print()

    cache_path = Path(__file__).resolve().parent.parent / ".cache"
    sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPES,
        cache_path=str(cache_path),
    ))

    user = sp.current_user()
    print(f"  OK: 認証成功！ユーザー = {user['display_name']} ({user['id']})")
    print(f"  OK: トークンキャッシュ = {cache_path}")
    print()
    return sp


def test_search_shows(sp):
    """ステップ3-pre: 利用可能なポッドキャスト番組を検索"""
    print("=" * 50)
    print("[ステップ3-pre] ポッドキャスト番組の検索")
    print("=" * 50)

    search_queries = [
        "BBC Global News Podcast",
        "TED Radio Hour",
        "6 Minute English BBC",
        "All Ears English",
    ]
    for query in search_queries:
        try:
            result = sp.search(q=query, type="show", market="JP", limit=3)
            shows = result.get("shows", {}).get("items", [])
            print(f"\n  検索: '{query}'")
            if not shows:
                print("    → 結果なし（日本リージョンで利用不可の可能性）")
            for s in shows:
                print(f"    - {s['name']}")
                print(f"      ID: {s['id']}  |  配信元: {s['publisher']}")
        except Exception as e:
            print(f"    → 検索失敗: {e}")
    print()


def test_fetch_episodes(sp):
    """ステップ3a: ポッドキャスト最新エピソード取得テスト"""
    print("=" * 50)
    print("[ステップ3a] ポッドキャスト最新エピソード取得")
    print("=" * 50)

    episodes = []
    for name, show_id in TEST_SHOWS.items():
        try:
            result = sp.show_episodes(show_id, limit=3, market="JP")
            items = result.get("items", [])
            print(f"\n  {name} (最新 {len(items)} 件):")
            for ep in items:
                dur_min = ep["duration_ms"] / 60000
                print(f"    - {ep['name'][:60]}  ({dur_min:.1f}分)")
                episodes.append(ep)
        except Exception as e:
            print(f"  NG: {name} の取得に失敗: {e}")

    print(f"\n  合計 {len(episodes)} エピソード取得")
    total_min = sum(ep["duration_ms"] for ep in episodes) / 60000
    print(f"  合計再生時間: {total_min:.1f} 分")
    print()
    return episodes


def test_playlist_update(sp, episodes):
    """ステップ3b: プレイリスト作成 & エピソード追加テスト"""
    print("=" * 50)
    print("[ステップ3b] プレイリスト更新テスト")
    print("=" * 50)

    user_id = sp.current_user()["id"]

    # 既存の "Morning English" プレイリストを検索
    playlist_id = None
    playlists = sp.current_user_playlists(limit=50)
    for pl in playlists["items"]:
        if pl["name"] == PLAYLIST_NAME and pl["owner"]["id"] == user_id:
            playlist_id = pl["id"]
            print(f"  OK: 既存プレイリスト発見: {PLAYLIST_NAME} ({playlist_id})")
            break

    # なければ新規作成
    if not playlist_id:
        print(f"  INFO: '{PLAYLIST_NAME}' が見つからないため新規作成します...")
        new_pl = sp.user_playlist_create(
            user_id, PLAYLIST_NAME,
            public=False,
            description="英語ポッドキャスト朝学習用（自動更新）",
        )
        playlist_id = new_pl["id"]
        print(f"  OK: プレイリスト作成完了: {playlist_id}")

    # エピソードから 90分以内に収まるよう選択
    selected = []
    total_ms = 0
    target_ms = 90 * 60 * 1000  # 90分
    for ep in episodes:
        if total_ms + ep["duration_ms"] <= target_ms:
            selected.append(ep)
            total_ms += ep["duration_ms"]

    uris = [ep["uri"] for ep in selected]
    print(f"  INFO: {len(selected)} エピソードを追加 (計 {total_ms / 60000:.1f} 分)")

    # プレイリストの中身を差し替え
    try:
        sp.playlist_replace_items(playlist_id, uris)
        print(f"  OK: プレイリスト更新成功！")
    except Exception as e:
        print(f"  NG: プレイリスト更新失敗: {e}")
        return None

    # 確認
    items = sp.playlist_items(playlist_id)
    print(f"  OK: プレイリスト内エピソード数 = {items['total']}")
    for item in items["items"]:
        track = item.get("track", {})
        name = track.get("name", "不明")
        dur = track.get("duration_ms", 0) / 60000
        print(f"    - {name[:60]}  ({dur:.1f}分)")

    print(f"\n  プレイリストID: {playlist_id}")
    print(f"  → .env に SPOTIFY_PLAYLIST_ID={playlist_id} を追記してください")
    print()
    return playlist_id


def main():
    print()
    print("🎧 Spotify API 検証スクリプト")
    print()

    check_env()
    sp = test_auth()
    test_search_shows(sp)
    episodes = test_fetch_episodes(sp)

    if not episodes:
        print("エピソードが取得できませんでした。show_id を確認してください。")
        sys.exit(1)

    playlist_id = test_playlist_update(sp, episodes)

    print("=" * 50)
    print("[検証結果サマリ]")
    print("=" * 50)
    if playlist_id:
        print("  全ステップ成功！")
        print()
        print("  次のステップ:")
        print("  1. Spotify アプリで 'Morning English' プレイリストを確認")
        print("  2. Alexa 定型アクションでこのプレイリストの再生を設定")
        print("  3. Alexa で再生できることを確認")
        print(f"  4. .env に SPOTIFY_PLAYLIST_ID={playlist_id} を追記")
    else:
        print("  一部ステップに失敗があります。上のログを確認してください。")


if __name__ == "__main__":
    main()
