# CLAUDE.md

## Project Overview

EnglishRadio — 毎朝の英語ラジオ。ポッドキャスト RSS から mp3 をダウンロードして貯め、新着優先＋ライブラリからのシャッフルで VLC 再生する。Flask の Web 管理画面（127.0.0.1:5210）付き。旧 `MorningPlaylist`（Spotify API）の後継。

## Running Commands

すべて `english_radio/` から venv の python で実行する（スクリプトは `scripts/_bootstrap.py` で sys.path と cwd を揃える）。

```powershell
.\venv\Scripts\python.exe scripts\morning_radio.py [--no-play] [--dry-run]
.\venv\Scripts\python.exe scripts\web_server.py
.\venv\Scripts\python.exe scripts\init_db.py
.\venv\Scripts\python.exe -m unittest discover -s tests -t .
```

## Architecture

```
scripts/morning_radio.py → src/radio.run()
  feeds.fetch_all()        RSS 取得（feedparser）。1番組の失敗はスキップ
  library.download_new()   未取得 guid だけ保存（.part → rename、2回リトライ、mutagen で長さ補完）
  selector.build_queue()   新着ラウンドロビン → 残りを未再生/古い順シャッフルで補充
  player.write_m3u/play()  data/today.m3u を VLC (--play-and-exit) で再生
  store.save_history()     play_history / play_history_items、episodes.last_played
  notifier.send_*()        Slack（shared/slack.py）
scripts/web_server.py → src/web/app.create_app()
  pages: / today history history/<id> library feeds schedule
  api:   /api/run, /api/play/*, /api/feeds(+preview), /api/schedules, /api/status
```

- 設定: `shared/config.py`（config.json）。相対パスは `src/paths.py` で english_radio/ 基準に解決
- DB: `shared/db.py` + `src/store.py`。`batch_run_log`（batch_type=`morning`）と `error_log` は watchdog の共通形式
- 時間割: `src/schedules.py`（検証・前後10分の一致判定・次回日時）。保存時に `scheduler_info.reregister()` が `setup_scheduler.ps1 -MorningOnly` を実行
- config 書き換えは `src/config_store.py`（検証 → 一時ファイル → os.replace）

## Rules

- mp3 は削除しない（ユーザー要望。貯めてシャッフル再生に使う）
- Web は 127.0.0.1 固定。Host ヘッダ検査と「変更系 API は JSON のみ」で他サイトからの操作を防いでいる。外さないこと
- 再生するパスは `paths.is_inside(path, library_dir)` で library 配下か確認してから VLC に渡す
- `--dry-run` は batch_run_log / error_log に書かない（watchdog の誤検知防止）
- テストは unittest。ネットワーク・VLC・タスクスケジューラはモックする。ログは `SHARED_LOG_DIR` で一時フォルダへ
