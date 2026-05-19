# Watchdog Dashboard - タスクスケジューラ登録
#
# 管理者権限の PowerShell で実行:
#   .\setup_scheduler.ps1

$WatchdogDir = $PSScriptRoot
$RepoRoot    = Split-Path $WatchdogDir -Parent
$PythonExe   = Join-Path $WatchdogDir ".venv\Scripts\python.exe"
$TaskName    = "Ishikawa238_WatchdogDashboard"

if (-not (Test-Path $PythonExe)) {
    Write-Host "SKIP: python.exe not found ($PythonExe)" -ForegroundColor Yellow
    Write-Host "  watchdog/.venv を作成してください" -ForegroundColor Yellow
    return
}

# 既存タスクがあれば削除
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task: $TaskName"
}

$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "run_dashboard.py" `
    -WorkingDirectory $WatchdogDir

$trigger = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -RunOnlyIfNetworkAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries

# 無期限実行（常駐サーバー）
$settings.ExecutionTimeLimit = "PT0S"

$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType S4U `
    -RunLevel Highest

Register-ScheduledTask `
    -TaskName    $TaskName `
    -Description "Ishikawa238 - Watchdog Dashboard (system status web UI, port 5200)" `
    -Action      $action `
    -Trigger     $trigger `
    -Settings    $settings `
    -Principal   $principal `
    -Force | Out-Null

Write-Host "Registered: $TaskName (at logon, persistent)" -ForegroundColor Green
