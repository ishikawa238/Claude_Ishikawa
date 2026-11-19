# english_radio

EnglishRadio の本体。コマンドはすべてこのフォルダ（`EnglishRadio/english_radio/`）で実行する。

## セットアップ

```powershell
# 1. venv と依存パッケージ
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. 設定ファイル（Slack の Webhook URL を入れる）
copy config_template.json config.json

# 3. DB 作成
.\venv\Scripts\python.exe scripts\init_db.py
```

4. **VLC をインストール**する（https://www.videolan.org/ 、インストール先は既定のままで OK）
5. **管理者権限の PowerShell** でタスクを登録する（初回だけ）

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\setup_scheduler.ps1
```

登録されるタスク:

| タスク | 内容 |
|---|---|
| `EnglishRadio_MorningPlay` | 時間割の各開始時刻に朝のラジオを再生（スリープ解除あり） |
| `EnglishRadio_WebServer` | ログオン時に Web 管理画面を起動（http://127.0.0.1:5210/） |

時間割を Web 画面で変えたときは、朝のタスクだけ自動で更新される（`setup_scheduler.ps1 -MorningOnly`）。

## 使い方

```powershell
# いますぐ流す（タスクスケジューラと同じ動き）
.\venv\Scripts\python.exe scripts\morning_radio.py

# ダウンロードと選曲だけ（再生・Slack なし）
.\venv\Scripts\python.exe scripts\morning_radio.py --no-play

# 試し実行（ダウンロードしない・Slack はコンソール表示・実行ログに残さない）
.\venv\Scripts\python.exe scripts\morning_radio.py --dry-run

# Web 管理画面を手動で起動
.\venv\Scripts\python.exe scripts\web_server.py
```

## テスト

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests -t .
```

ネットワーク・VLC・タスクスケジューラはモックしているので、オフラインでも動く。

## 設定ファイル（config.json）

| キー | 内容 |
|---|---|
| `radio.schedules` | 時間割。`{"label", "start", "end", "days"}`。`days` は `daily` / `weekdays` / `weekends` |
| `radio.target_minutes` | 時間割の外で手動実行したときの長さ（分） |
| `radio.episodes_per_feed` | 1番組あたり毎回チェックする最新エピソード数 |
| `radio.feeds` | 番組リスト。`{"name", "url", "enabled"}` |
| `radio.vlc_path` | VLC の場所。見つからなければ標準のインストール先と PATH も探す |
| `paths.*` | DB・ライブラリ・m3u の置き場所（english_radio/ からの相対パス） |
| `slack.webhook_url` | Slack 通知先。空なら通知をコンソールに表示 |

### 初期の番組

| 番組 | 備考 |
|---|---|
| NPR Up First | 約 13 分のニュース |
| BBC Global News Podcast | 約 27 分のニュース |
| TED Radio Hour | 約 50 分 |
| 6 Minute English (BBC) | 約 6 分の英語学習 |
| All Ears English | 約 20 分の英語学習 |
| 英語聞き流し \| Sakura English | **初期はお休み**。1話が約 2 時間あり、90 分の枠に入らないため（番組画面でオンにできる） |

## データ

| 場所 | 内容 |
|---|---|
| `library/<番組>/<配信日>_<タイトル>.mp3` | ダウンロードした mp3（削除しない） |
| `data/english_radio.db` | エピソード・再生履歴・実行ログ（watchdog が監視） |
| `data/today.m3u` | 今朝のリスト（VLC で開くとそのまま再生できる） |
| `logs/batch_YYYYMMDD.log` | 実行ログ |

## トラブルシューティング

- **再生されない / 「VLC が見つかりません」**: VLC をインストールする。別の場所に入れた場合は `radio.vlc_path` を設定
- **ダウンロード失敗の警告**: 配信元の一時的な遅延。2回まで試し、だめなら翌日に自動でやり直す
- **Web 画面で時間割を保存したら「反映に失敗」**: 管理者 PowerShell で `setup_scheduler.ps1` を実行
- **Echo で聞きたい**: PC と Echo を Bluetooth でペアリングし、Echo を PC のスピーカーにする
