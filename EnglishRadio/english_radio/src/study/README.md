# src/study

EnglishRadio の英語学習機能（文字起こし・和訳・単語帳）。

| ファイル | 内容 |
|---|---|
| `transcribe.py` | faster-whisper で PC 内文字起こし。単語ごとの時刻で「1文 = 1セグメント」にまとめる |
| `ai.py` | Claude（structured outputs）で和訳・日本語要約・覚えたい単語/フレーズを作る |
| `runner.py` | 学習バッチ本体。対象選び → 文字起こし → AI → 保存。batch_run_log は `study` |
| `store.py` | `transcripts` / `transcript_segments` / `vocab` テーブルと Leitner 方式の復習間隔 |

Web 画面は `src/web/study_routes.py`、入口は `scripts/study_batch.py`。
