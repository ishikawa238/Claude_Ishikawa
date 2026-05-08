# AutoCommit

毎日深夜 3:00 にリポジトリの変更を検知し、自動で git commit & push を行うシステム。

## 概要

- `git status` で未コミットの変更（untracked 含む）を検知
- 変更があれば `git add -A` → `git commit` → `git push` を実行
- コミットメッセージは変更ディレクトリ・ファイル数から自動生成
- 結果を Slack に通知（変更があった場合のみ）

## セットアップ

```powershell
cd auto_commit
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install -e ../[slack]
copy config_template.json config.json
# config.json の webhook_url を設定
```

## 手動実行

```powershell
cd auto_commit
.venv\Scripts\python.exe run_auto_commit.py
```

## タスクスケジューラ登録

リポジトリルートで管理者権限の PowerShell から:

```powershell
.\setup_all.ps1
```
