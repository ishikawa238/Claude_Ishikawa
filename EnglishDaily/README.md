# EnglishDaily

英語学習の継続支援システム。

毎朝 Slack に曜日別の学習タスクを配信し、リアクションで完了を追跡する。未完了タスクには夕方から段階的にリマインド通知を送信。

## 機能

- 曜日ごとの学習カリキュラム（単語・リスニング・シャドウイング・ライティング）
- Slack リアクションによる個別タスク完了追跡
- 17:30〜23:30 の段階的リマインド（トーンが徐々に強くなる）
- 連続学習日数（ストリーク）の自動集計・表示

## セットアップ

```bash
cd english_daily
python -m venv .venv
.venv\Scripts\activate.bat
pip install -e "../../[slack]"

copy config_template.json config.json   # Bot Token とチャンネルを設定
python -m scripts.init_db
```

### Slack App の準備

1. https://api.slack.com/apps で App を作成
2. OAuth & Permissions で `chat:write` と `reactions:read` スコープを追加
3. Install to Workspace で Bot Token (`xoxb-...`) を取得
4. 通知先チャンネルで `/invite @アプリ名` を実行
5. `config.json` に Bot Token とチャンネル名を記入

## 使い方

```bash
# 手動実行
python -m scripts.batch_morning    # 朝の配信
python -m scripts.batch_reminder   # リマインド

# タスクスケジューラ登録（管理者 PowerShell）
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\setup_scheduler.ps1
```

## カリキュラム変更

`config.json` の `curriculum` セクションを編集する。曜日ごとにタスクのリストを自由に設定できる。
