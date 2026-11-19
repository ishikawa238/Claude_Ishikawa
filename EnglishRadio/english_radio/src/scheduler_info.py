"""
Windows タスクスケジューラの状態取得と再登録（Web 管理画面用）。

日本語環境の schtasks 出力は列名がローカライズされるため、
PowerShell の Get-ScheduledTask を JSON で受け取る。
"""

import json
import subprocess
import sys

from src.paths import PROJECT_ROOT

MORNING_TASK = "EnglishRadio_MorningPlay"
WEB_TASK = "EnglishRadio_WebServer"
_TIMEOUT_SEC = 60

_STATUS_PS = r"""
$ErrorActionPreference = 'Stop'
$t = Get-ScheduledTask -TaskName '{name}'
$i = $t | Get-ScheduledTaskInfo
$fmt = 'yyyy-MM-dd HH:mm'
[pscustomobject]@{{
  State = [string]$t.State
  LastRunTime = if ($i.LastRunTime -and $i.LastRunTime.Year -gt 2000) {{ $i.LastRunTime.ToString($fmt) }} else {{ '' }}
  LastTaskResult = $i.LastTaskResult
  NextRunTime = if ($i.NextRunTime) {{ $i.NextRunTime.ToString($fmt) }} else {{ '' }}
}} | ConvertTo-Json -Compress
"""


def _powershell(args: list[str]) -> subprocess.CompletedProcess:
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", *args],
        capture_output=True,
        timeout=_TIMEOUT_SEC,
        creationflags=flags,
    )


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def get_task_status(task_name: str) -> dict:
    """{"exists", "state", "last_run", "last_result", "next_run"} を返す。"""
    if task_name not in (MORNING_TASK, WEB_TASK):
        raise ValueError("未知のタスク名です")
    try:
        proc = _powershell(["-Command", _STATUS_PS.format(name=task_name)])
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"exists": False, "error": str(e)}
    if proc.returncode != 0:
        return {"exists": False, "error": "タスクが登録されていません"}
    try:
        data = json.loads(_decode(proc.stdout))
    except ValueError:
        return {"exists": False, "error": "タスクの状態を読み取れませんでした"}
    return {
        "exists": True,
        "state": data.get("State", ""),
        "last_run": data.get("LastRunTime", ""),
        "last_result": data.get("LastTaskResult"),
        "next_run": data.get("NextRunTime", ""),
    }


def reregister() -> tuple[bool, str]:
    """朝のタスクだけを setup_scheduler.ps1 で再登録する（管理者権限なしで動く範囲）。"""
    script = PROJECT_ROOT / "setup_scheduler.ps1"
    try:
        proc = _powershell(["-ExecutionPolicy", "Bypass", "-File", str(script), "-MorningOnly"])
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    output = (_decode(proc.stdout) + _decode(proc.stderr)).strip()
    return proc.returncode == 0, output[-2000:]
