# MorningPlaylist

英語ポッドキャストの最新エピソードで Spotify プレイリスト「Morning English」を毎朝自動更新し、Spotify Connect で再生を開始する。

## 仕組み

1. 指定したポッドキャスト番組から最新エピソードを取得
2. 合計再生時間が約90分に収まるよう選択（ラウンドロビンで各番組から均等に）
3. プレイリストの中身を差し替え
4. Spotify Connect で Echo（または PC）に再生開始
5. 結果を Slack に通知

## 対象番組（デフォルト）

- NPR Up First
- BBC Global News Podcast
- TED Radio Hour
- 6 Minute English (BBC)
- All Ears English

`config.json` の `spotify.shows` で追加・変更可能。

## セットアップ

```powershell
cd MorningPlaylist\morning_playlist

# venv 作成 & 依存インストール
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt

# .env 作成（Spotify Developer Dashboard で取得した値を入力）
# .env.example を参考に作成

# 初回認証（ブラウザが開くので Spotify にログインして認可）
.\venv\Scripts\python.exe scripts\update_playlist.py

# タスクスケジューラ登録（管理者権限の PowerShell で実行）
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\setup_scheduler.ps1
```

## 設定ファイル

| ファイル | 内容 |
|---|---|
| `.env` | Spotify API 認証情報（`SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REDIRECT_URI`） |
| `config.json` | 番組リスト、プレイリストID、再生デバイス名、Slack Webhook URL |
| `.cache` | OAuth トークンキャッシュ（自動生成） |

## 再生デバイスの切り替え

`config.json` の `spotify.device_name` を変更する。デバイス名は部分一致で検索される。

- Echo で再生: `"device_name": "Echo"`
- PC で再生: `"device_name": "PC名"` または Spotify アプリに表示されるデバイス名

## タスクスケジューラ

- タスク名: `MorningEnglishPlaylistUpdate`
- 実行時刻: `config.json` の `schedules` に登録された各スケジュールの開始時刻（デフォルト: 7:00）
- スリープ解除: 有効

スケジュールを変更した場合は `setup_scheduler.ps1` を再実行してトリガーを更新する。

```powershell
# 確認
Get-ScheduledTask -TaskName "MorningEnglishPlaylistUpdate"

# 手動実行
Start-ScheduledTask -TaskName "MorningEnglishPlaylistUpdate"
```

## ポッドキャスト設定 GUI

ブラウザから番組の追加・削除・保存ができる。

```powershell
# サーバー起動（morning_playlist/ から）
.\venv\Scripts\python.exe scripts\podcast_selector.py
```

ブラウザで http://127.0.0.1:5200/ を開く。番組の追加・削除に加え、スケジュール（何時から何時まで再生するか）の設定もできる。変更は次回のプレイリスト更新から反映される。

## トラブルシューティング

- **トークン期限切れ**: `.cache` を削除して `update_playlist.py` を手動実行し再認証
- **デバイスが見つからない**: Echo で何か再生してデバイスをアクティブにしてから実行
- **ログ**: `morning_playlist/logs/batch_YYYYMMDD.log` を確認
