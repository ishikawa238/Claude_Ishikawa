# Watchdog

全自動化システムの横断監視とステータスダッシュボード。

## 機能

| スクリプト | 概要 |
|-----------|------|
| `run_watchdog.py` | 全プロジェクトの SQLite DB を巡回し、バッチ未実行・致命的エラーを検知して Slack に通知する |
| `run_dashboard.py` | ブラウザで全システムの稼働状況を一覧表示する Web ダッシュボード (Flask, port 5200) |

## セットアップ

```bash
cd watchdog
python -m venv .venv
.venv\Scripts\activate.bat
pip install -e "../../[slack]" -r requirements.txt
pip install flask

copy config_template.json config.json   # Slack webhook URL を設定
```

## 実行

```bash
# Watchdog（Slack 通知）
python run_watchdog.py

# ダッシュボード（Web UI）
python run_dashboard.py
# → http://127.0.0.1:5200/
```

## ダッシュボード

- 全システムのバッチ状態・致命的エラー・ログファイル有無をカードで一覧表示
- 60 秒間隔で自動リフレッシュ
- ダーク/ライトテーマ切替
- 未登録システムの自動発見

## テスト

```bash
python -m pytest tests/ -v
```

## タスクスケジューラ

| タスク名 | トリガー |
|---------|----------|
| `Ishikawa238_Watchdog` | 毎日 22:00 |
| `Ishikawa238_WatchdogDashboard` | ログオン時（常駐） |
