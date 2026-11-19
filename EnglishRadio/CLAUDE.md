# CLAUDE.md

## Project Overview

EnglishRadio — 毎朝の英語ラジオ。ポッドキャスト RSS から mp3 をダウンロードして貯め、新着優先＋ライブラリからのシャッフルで VLC 再生する。Flask の Web 管理画面（127.0.0.1:5210）付き。旧 `MorningPlaylist`（Spotify API）の後継。

## Running Commands

すべて `english_radio/` から venv の python で実行する（スクリプトは `scripts/_bootstrap.py` で sys.path と cwd を揃える）。

```powershell
.\venv\Scripts\python.exe scripts\morning_radio.py [--no-play] [--dry-run]
.\venv\Scripts\python.exe scripts\web_server.py
.\venv\Scripts\python.exe scripts\study_batch.py [--guid <guid>] [--dry-run]
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
  radio.refresh_artwork()  番組画像・エピソード画像を artwork.cache_image() で 600px JPEG にキャッシュ（失敗しても再生は続行）
scripts/study_batch.py → src/study/runner.run()   毎日 09:00（EnglishRadio_StudyBatch）＋Web から1話ずつ
  transcribe.transcribe()  faster-whisper（CPU int8、既定 small.en）→ 単語ごとの時刻で1文ずつにまとめる
  ai.translate()           Claude（既定 claude-haiku-5-5）に40文ずつ送り、番号つき JSON で和訳（structured outputs）
  ai.extract_study()       1話1回: 日本語要約＋単語/フレーズ最大15件（例文は文番号でリンク）
  study/store.py           transcripts / transcript_segments / vocab（Leitner 方式: 1/2/4/7/15/30日）
scripts/web_server.py → src/web/app.create_app()
  pages: / today history history/<id> library feeds schedule releases（templates/_macros.html の artwork/track/ecard/mosaic を共通利用）
  study: /study /study/episode?guid= /vocab（src/web/study_routes.py。guid は URL を含むことがあるのでクエリで渡す）
  audio: /audio?guid=（library 配下か確認して send_file、Range 対応）
  art:   /art/<sha1>.jpg（名前は正規表現で検証して data/artwork/ から配信）
  api:   /api/run, /api/play/*, /api/feeds(+preview), /api/schedules, /api/status, /api/study/*, /api/vocab/<id>
```

- 設定: `shared/config.py`（config.json）。相対パスは `src/paths.py` で english_radio/ 基準に解決
- DB: `shared/db.py` + `src/store.py`（学習系は `src/study/store.py`）。`batch_run_log`（batch_type=`morning` / `study`）と `error_log` は watchdog の共通形式。ダッシュボードの「今日の状態」は morning だけを見る
- リリースノート: `english_radio/RELEASE_NOTES.md` を `src/release_notes.py` が読んで /releases に表示
- 時間割: `src/schedules.py`（検証・前後10分の一致判定・次回日時）。保存時に `scheduler_info.reregister()` が `setup_scheduler.ps1 -MorningOnly` を実行
- config 書き換えは `src/config_store.py`（検証 → 一時ファイル → os.replace）

## Rules

- mp3 は削除しない（ユーザー要望。貯めてシャッフル再生に使う）
- Web は 127.0.0.1 固定。Host ヘッダ検査と「変更系 API は JSON のみ」で他サイトからの操作を防いでいる。外さないこと
- 再生するパスは `paths.is_inside(path, library_dir)` で library 配下か確認してから VLC に渡す
- `--dry-run` は batch_run_log / error_log に書かない（watchdog の誤検知防止）
- 機能を追加・変更したら `english_radio/RELEASE_NOTES.md` の先頭に版を追加する（`## v<版> — <YYYY-MM-DD> <タイトル>` ＋ `- ` の箇条書き）
- 学習バッチは1話の失敗で止めない（文字起こしは最大3回、AI の失敗は英文を残して次回やり直し）。faster-whisper 未導入（ImportError）だけは全体の失敗にする
- PyAV は 15 系に固定（faster-whisper 1.2.1 が av 16 以降の av.open() 引数変更に未対応）
- テストは unittest。ネットワーク・VLC・タスクスケジューラ・Whisper・Claude はモックする。ログは `SHARED_LOG_DIR` で一時フォルダへ
