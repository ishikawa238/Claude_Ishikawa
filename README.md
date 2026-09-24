# Ishikawa238

個人プロジェクト群のモノレポ。情報収集・投資支援・学習習慣づくりなど、日常を自動化する Python システムをまとめている。各システムは Windows タスクスケジューラで定期実行し、結果を Slack に通知する共通アーキテクチャを持つ。

## フォルダ構成

| フォルダ | 概要 |
|----------|------|
| `Investment/` | 日米株式の投資判断支援システム。テクニカル指標の算出、売買シグナル検知、保有銘柄の監視を行い Slack に通知する。 |
| `AiNewsDigest/` | AI 関連ニュース自動収集システム。複数の RSS フィードから AI/ML/LLM ニュースを取得し、重複排除の上 Slack に通知する。 |
| `PaperWatch/` | 新着論文監視システム。arXiv RSS から指定カテゴリの論文を取得し、キーワードスコアリングで関連度の高い論文を Slack に通知する。 |
| `FreelanceScout/` | フリーランス案件自動収集システム。CrowdWorks・Coconala から IT/AI 系案件をスクレイプし、キーワードフィルタ後に Slack 通知する。 |
| `EnglishDaily/` | 英語学習習慣化システム。曜日別カリキュラムを毎朝 Slack に配信し、連続学習日数（ストリーク）を管理する。 |
| `MorningPlaylist/` | 英語ポッドキャスト自動再生システム。Spotify API で最新エピソードをプレイリストに入れ替え、Spotify Connect で Echo に再生開始する。 |
| `watchdog/` | 横断監視スクリプト。全システムの SQLite DB を巡回し、バッチ未実行や致命的エラーを検知して Slack に通知する。 |
| `shared/` | 共通モジュール（config, db, slack, logger, net）。各システムから `pip install -e "../../[slack]"` で利用する。`logger` は環境変数 `SHARED_LOG_DIR` でログ出力先を上書きできる（テスト用）。 |
| `claude-study/` | Claude Code の練習用プロジェクト。CLI 電卓と CSV 集計ツール。 |

## Slack 通知スケジュール

| 時刻 | システム | 通知内容 |
|------|----------|----------|
| 毎日 07:00 | AiNewsDigest | AI/ML/LLM 関連ニュースの新着ダイジェスト |
| 毎日 07:00 | FreelanceScout | CrowdWorks・Coconala の新着フリーランス案件 |
| 毎日 07:30 | Investment | 米国株シグナル + 前夜の日本株シグナル統合 + 保有銘柄アラート（朝レポート） |
| 毎日 18:30 | Investment | 日本株のテクニカル分析・売買シグナル検知結果（夕方レポート） |
| 毎月 1日 08:00 | Investment | 月次レポート（銘柄マスタ更新 + ファンダメンタルズ取得） |
| 毎日 07:00 | PaperWatch | arXiv 新着論文のキーワードマッチレポート |
| 毎日 07:00 | MorningPlaylist | 英語ポッドキャストプレイリスト自動更新 & Echo 再生 |
| 毎日 07:00 | EnglishDaily | 曜日別英語学習カリキュラム + ストリーク通知 |
| 毎日 17:30 / 19:30 / 21:30 / 23:30 | EnglishDaily | 未完了タスクのリマインド通知 |
| 随時（異常時のみ） | watchdog | バッチ未実行・致命的エラーの検知アラート |

## 共通アーキテクチャ

- **言語**: Python 3.10+
- **データベース**: SQLite（WAL モード）
- **通知**: Slack Incoming Webhook
- **スケジューリング**: Windows タスクスケジューラ
- **共通基盤**: `shared/` パッケージ（`pyproject.toml` で管理）

## セットアップ（共通手順）

```bash
# 1. shared パッケージをインストール（リポジトリルートで）
pip install -e ".[slack]"

# 2. 各プロジェクトへ移動してセットアップ
cd <project>/<inner_dir>
python -m venv .venv
.venv\Scripts\activate.bat
pip install -e "../../[slack]" -r requirements.txt
copy config_template.json config.json   # API キー等を設定
python -m scripts.init_db
```

詳細は各フォルダの `CLAUDE.md` または `README.md` を参照。

## タスクスケジューラ一括登録

全システムのタスクスケジューラを一括で登録する。初回セットアップ時、またはタスクを再登録したいときに実行する。

```powershell
# 管理者権限の PowerShell で実行
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\setup_all.ps1
```

登録されるタスク:

| タスク | トリガー |
|--------|----------|
| Investment_Dashboard | ログオン時（常駐サーバー） |
| Investment_MorningBatch_ALL | 毎日 07:30 |
| Investment_EveningBatch_JP | 毎日 18:30 |
| Investment_MonthlyBatch | 毎月 1日 08:00 |
| Investment_OpenD | ログオン時 + 毎時 10分（ログオン中のみ。moomoo OpenD が落ちていれば起動） |
| MorningEnglishPlaylistUpdate | config.json で指定した時刻 |
| FreelanceScout_MorningBatch | 毎日 07:00 |
| AiNewsDigest_MorningBatch | 毎日 07:00 |
| EnglishDaily_MorningBatch | 毎日 07:00 |
| EnglishDaily_Reminder_* | 毎日 17:30 / 19:30 / 21:30 / 23:30 |
| PaperWatch_MorningBatch | 毎日 07:00 |
| Ishikawa238_Watchdog | 毎日 22:00 |

## 稼働状態チェック

PC 再起動後などに、全タスクの登録状態と最終実行結果を一覧で確認する。

```powershell
# 管理者権限不要
.\status.ps1
```

未登録タスクがある場合は `.\setup_all.ps1` を実行するようガイドが表示される。
