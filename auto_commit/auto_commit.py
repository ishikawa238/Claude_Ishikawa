"""
AutoCommit — リポジトリ変更の自動コミット＆プッシュ

毎日深夜 3:00 に実行し、未コミットの変更があれば
自動で git add → commit → push を行う。

実行方法（auto_commit/ から）:
    python run_auto_commit.py
"""

import subprocess
from datetime import datetime
from pathlib import Path

from shared import slack
from shared.config import get_config
from shared.logger import get_logger

logger = get_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(*args: str) -> subprocess.CompletedProcess:
    """リポジトリルートで git コマンドを実行する。"""
    return subprocess.run(
        ["git", *args],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def get_changed_files() -> list[str]:
    """未コミットの変更ファイル一覧を取得する（untracked 含む）。"""
    result = _git("status", "--porcelain")
    if result.returncode != 0:
        logger.error(f"git status 失敗: {result.stderr.strip()}")
        return []

    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    return lines


def _summarize_changes(changed_lines: list[str]) -> str:
    """変更ファイル一覧から簡潔なコミットメッセージを生成する。"""
    files = []
    for line in changed_lines:
        # git status --porcelain: "XY filename" or "XY filename -> newname"
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2:
            files.append(parts[1])

    if not files:
        return "Auto-commit: minor changes"

    # 変更されたトップレベルディレクトリを集約
    top_dirs = set()
    for f in files:
        # "dir/..." → "dir", ルート直下のファイルはファイル名
        first = f.split("/")[0]
        top_dirs.add(first)

    dirs_str = ", ".join(sorted(top_dirs))
    return f"Auto-commit ({datetime.now():%Y-%m-%d}): {dirs_str} ({len(files)} files)"


def commit_and_push() -> dict:
    """
    変更を検知し、コミット＆プッシュを行う。

    返り値:
        {
            "has_changes": bool,
            "committed": bool,
            "pushed": bool,
            "message": str,
            "file_count": int,
            "error": str | None,
        }
    """
    result = {
        "has_changes": False,
        "committed": False,
        "pushed": False,
        "message": "",
        "file_count": 0,
        "error": None,
    }

    # 1. 変更検知
    changed = get_changed_files()
    if not changed:
        logger.info("変更なし — スキップ")
        return result

    result["has_changes"] = True
    result["file_count"] = len(changed)
    logger.info(f"変更検知: {len(changed)} ファイル")
    for line in changed[:20]:
        logger.info(f"  {line}")
    if len(changed) > 20:
        logger.info(f"  ... 他 {len(changed) - 20} ファイル")

    # 2. ステージング
    add_result = _git("add", "-A")
    if add_result.returncode != 0:
        err = f"git add 失敗: {add_result.stderr.strip()}"
        logger.error(err)
        result["error"] = err
        return result

    # 3. コミット
    commit_msg = _summarize_changes(changed)
    result["message"] = commit_msg
    commit_result = _git("commit", "-m", commit_msg)
    if commit_result.returncode != 0:
        stderr = commit_result.stderr.strip()
        # "nothing to commit" は正常系として扱う
        if "nothing to commit" in commit_result.stdout:
            logger.info("ステージング後に差分なし — スキップ")
            result["has_changes"] = False
            return result
        err = f"git commit 失敗: {stderr}"
        logger.error(err)
        result["error"] = err
        return result

    result["committed"] = True
    logger.info(f"コミット完了: {commit_msg}")

    # 4. プッシュ
    push_result = _git("push")
    if push_result.returncode != 0:
        err = f"git push 失敗: {push_result.stderr.strip()}"
        logger.error(err)
        result["error"] = err
        return result

    result["pushed"] = True
    logger.info("プッシュ完了")

    return result


# ---------------------------------------------------------------------------
# Slack 通知
# ---------------------------------------------------------------------------


def build_slack_blocks(result: dict, run_time: str) -> list[dict]:
    """コミット結果から Slack Block Kit の blocks を組み立てる。"""
    if not result["has_changes"]:
        return []  # 変更なしなら通知しない

    if result["committed"] and result["pushed"]:
        header = f"*[AutoCommit] コミット＆プッシュ完了  {run_time}*"
        icon = "OK"
    elif result["committed"]:
        header = f"*[AutoCommit] コミット済み・プッシュ失敗  {run_time}*"
        icon = "WARN"
    else:
        header = f"*[AutoCommit] コミット失敗  {run_time}*"
        icon = "NG"

    lines = [
        f"`status  ` [{icon}]",
        f"`files   ` {result['file_count']} ファイル",
        f"`message ` {result['message']}",
    ]
    if result["error"]:
        lines.append(f"`error   ` {result['error'][:200]}")

    blocks = [
        slack.section(header),
        slack.divider(),
        slack.section("\n".join(lines)),
        slack.divider(),
    ]
    return blocks


# ---------------------------------------------------------------------------
# メインエントリーポイント
# ---------------------------------------------------------------------------


def run() -> int:
    """
    変更検知 → コミット → プッシュ → Slack 通知。

    返り値:
        0 = 正常終了（変更なし or コミット＆プッシュ成功）
        1 = コミットまたはプッシュ失敗
        2 = auto_commit 自体のエラー
    """
    cfg = get_config()
    webhook_url = cfg.get("slack", {}).get("webhook_url", "")

    run_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    logger.info(f"[AutoCommit] 開始: {run_time}")

    result = commit_and_push()

    # Slack 通知（変更があった場合のみ）
    blocks = build_slack_blocks(result, run_time)
    if blocks:
        payload = {"blocks": blocks}
        slack.send_webhook(webhook_url, payload, logger=logger)

    if result["error"]:
        return 1
    return 0
