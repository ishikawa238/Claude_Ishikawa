"""
Spotify API 技術検証スクリプト

以下の3点を検証する:
1. OAuth認証が通るか
2. ポッドキャストのエピソード一覧を取得できるか
3. プレイリストにポッドキャストエピソードを追加できるか

使い方:
  1. 環境変数を設定:
     export SPOTIPY_CLIENT_ID="your_client_id"
     export SPOTIPY_CLIENT_SECRET="your_client_secret"
     export SPOTIPY_REDIRECT_URI="http://localhost:8888/callback"
     export SPOTIFY_PLAYLIST_ID="your_playlist_id"

  2. 実行:
     python verify_spotify_api.py

  初回実行時はブラウザが開き、Spotify認証を求められます。
  認証後、リダイレクト先URLをコピーしてターミナルに貼り付けてください。

  ※ SPOTIPY_REDIRECT_URI は Spotify Developer Dashboard の
    Redirect URI と完全一致させてください。
    例: http://127.0.0.1:8888/callback または https://localhost:8888/callback
"""

import os
import sys

# Windows コンソールの文字コード問題を回避
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import spotipy
from spotipy.oauth2 import SpotifyOAuth

# --- 設定 ---
SCOPES = "playlist-modify-public playlist-modify-private playlist-read-private"

# 検証用ポッドキャスト（Spotify Show ID）
TEST_SHOWS = {
    "NPR Up First": "2mTUnDkuKUkhiueKcVWoP0",
    "BBC Global News Podcast": "2cjOiq0IMn3qmUFGj6MvsT",
    "TED Radio Hour": "4qsGhJJEBMSYBOGKsYWJcw",
}

PLAYLIST_ID = os.environ.get("SPOTIFY_PLAYLIST_ID", "")


def step1_authenticate():
    """Step 1: OAuth認証の検証"""
    print("=" * 60)
    print("Step 1: OAuth 認証")
    print("=" * 60)

    try:
        sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
            scope=SCOPES,
            open_browser=False,
        ))
        user = sp.current_user()
        print(f"  [OK] 認証成功: {user['display_name']} ({user['id']})")
        return sp
    except Exception as e:
        print(f"  [NG] 認証失敗: {e}")
        return None


def step2_get_episodes(sp):
    """Step 2: ポッドキャストエピソード取得の検証"""
    print()
    print("=" * 60)
    print("Step 2: ポッドキャストエピソード取得")
    print("=" * 60)

    episodes = []
    for name, show_id in TEST_SHOWS.items():
        try:
            result = sp.show_episodes(show_id, limit=3, market="JP")
            items = result.get("items", [])
            if items:
                latest = items[0]
                duration_min = latest["duration_ms"] / 1000 / 60
                print(f"  [OK] {name}")
                print(f"       最新: {latest['name']}")
                print(f"       公開日: {latest['release_date']}")
                print(f"       長さ: {duration_min:.1f}分")
                print(f"       URI: {latest['uri']}")
                episodes.append(latest)
            else:
                print(f"  [NG] {name}: エピソードが空")
        except Exception as e:
            print(f"  [NG] {name}: {e}")

    return episodes


def step3_add_to_playlist(sp, episodes):
    """Step 3: プレイリストへのエピソード追加の検証"""
    print()
    print("=" * 60)
    print("Step 3: プレイリストへのエピソード追加")
    print("=" * 60)

    if not PLAYLIST_ID:
        print("  [SKIP] SPOTIFY_PLAYLIST_ID が未設定です。")
        print("  設定方法: export SPOTIFY_PLAYLIST_ID=\"your_playlist_id\"")
        print()
        print("  プレイリストIDの確認方法:")
        print("  Spotifyアプリでプレイリストを右クリック →")
        print("  「シェア」→「リンクをコピー」→ URLの末尾がID")
        print("  例: https://open.spotify.com/playlist/XXXXX → XXXXX がID")
        return False

    if not episodes:
        print("  [SKIP] 追加するエピソードがありません。")
        return False

    # テスト用に最初の1エピソードだけ追加
    test_episode = episodes[0]
    test_uri = test_episode["uri"]

    try:
        # 現在のプレイリスト内容を取得
        current = sp.playlist_items(PLAYLIST_ID, fields="items(track(uri))")
        current_uris = [
            item["track"]["uri"]
            for item in current.get("items", [])
            if item.get("track")
        ]
        print(f"  現在のプレイリスト: {len(current_uris)} アイテム")

        # エピソードを1つ追加
        sp.playlist_add_items(PLAYLIST_ID, [test_uri])
        print(f"  [OK] エピソード追加成功: {test_episode['name']}")

        # 追加されたか確認
        updated = sp.playlist_items(PLAYLIST_ID, fields="total")
        print(f"  確認: プレイリストアイテム数 = {updated['total']}")

        # テスト追加分を削除
        sp.playlist_remove_all_occurrences_of_items(PLAYLIST_ID, [test_uri])
        print(f"  [OK] テスト追加分を削除しました")

        return True
    except Exception as e:
        print(f"  [NG] プレイリスト操作失敗: {e}")
        print(f"       エラー型: {type(e).__name__}")
        if hasattr(e, "http_status"):
            print(f"       HTTPステータス: {e.http_status}")
        if hasattr(e, "msg"):
            print(f"       メッセージ: {e.msg}")
        return False


def main():
    print()
    print("Spotify API 技術検証")
    print("=" * 60)
    print()

    # Step 1: 認証
    sp = step1_authenticate()
    if sp is None:
        print("\n認証に失敗しました。環境変数を確認してください。")
        sys.exit(1)

    # Step 2: エピソード取得
    episodes = step2_get_episodes(sp)

    # Step 3: プレイリスト操作
    playlist_ok = step3_add_to_playlist(sp, episodes)

    # 結果サマリ
    print()
    print("=" * 60)
    print("検証結果サマリ")
    print("=" * 60)
    print(f"  認証:           OK")
    print(f"  エピソード取得: {len(episodes)}/{len(TEST_SHOWS)} 番組成功")
    print(f"  プレイリスト:   {'OK' if playlist_ok else 'SKIP/NG'}")
    print()

    if len(episodes) > 0 and playlist_ok:
        print(">>> 全検証クリア! MVP実装に進めます。")
    elif len(episodes) > 0 and not PLAYLIST_ID:
        print(">>> エピソード取得は成功。")
        print(">>> SPOTIFY_PLAYLIST_ID を設定してプレイリスト操作も検証してください。")
    else:
        print(">>> 一部検証に失敗しました。上記のエラーを確認してください。")


if __name__ == "__main__":
    main()
