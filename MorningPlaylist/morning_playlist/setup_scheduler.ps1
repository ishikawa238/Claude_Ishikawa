# Morning English プレイリスト更新 - タスクスケジューラ登録スクリプト
#
# 管理者権限の PowerShell で実行してください:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\setup_scheduler.ps1

$TaskName = "MorningEnglishPlaylistUpdate"
$PythonExe = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
$ScriptPath = Join-Path $PSScriptRoot "scripts\update_playlist.py"
$WorkDir = $PSScriptRoot

Write-Host "python : $PythonExe"
Write-Host "script : $ScriptPath"
Write-Host "workdir: $WorkDir"

if (-not (Test-Path $PythonExe)) {
    Write-Host "ERROR: python.exe not found at $PythonExe" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $ScriptPath)) {
    Write-Host "ERROR: script not found at $ScriptPath" -ForegroundColor Red
    exit 1
}

# Task Scheduler COM API constants
$TASK_ACTION_EXEC = 0
$TASK_TRIGGER_DAILY = 2
$TASK_CREATE_OR_UPDATE = 6
$TASK_LOGON_INTERACTIVE_TOKEN = 3
$TASK_RUNLEVEL_LUA = 0

try {
    $service = New-Object -ComObject "Schedule.Service"
    $service.Connect()
    $rootFolder = $service.GetFolder("\")

    $definition = $service.NewTask(0)
    $definition.RegistrationInfo.Description = "Morning English: Spotify playlist auto update and playback"

    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $definition.Principal.UserId = $currentUser
    $definition.Principal.LogonType = $TASK_LOGON_INTERACTIVE_TOKEN
    $definition.Principal.RunLevel = $TASK_RUNLEVEL_LUA

    # Settings: wake the computer and run even when on battery.
    $definition.Settings.Enabled = $true
    $definition.Settings.StartWhenAvailable = $true
    $definition.Settings.WakeToRun = $true
    $definition.Settings.DisallowStartIfOnBatteries = $false
    $definition.Settings.StopIfGoingOnBatteries = $false

    # config.json からスケジュールを読み込み、各開始時刻にトリガーを作成
    $configPath = Join-Path $PSScriptRoot "config.json"
    $schedTimes = @()
    if (Test-Path $configPath) {
        $cfg = Get-Content $configPath -Raw | ConvertFrom-Json
        if ($cfg.spotify.schedules) {
            foreach ($s in $cfg.spotify.schedules) {
                $schedTimes += $s.start
            }
        }
    }
    # スケジュールが無ければ 07:00 をデフォルトにする
    if ($schedTimes.Count -eq 0) {
        $schedTimes = @("07:00")
    }

    foreach ($t in $schedTimes) {
        $parts = $t -split ":"
        $hour = [int]$parts[0]
        $minute = [int]$parts[1]
        $startAt = (Get-Date).Date.AddHours($hour).AddMinutes($minute)
        if ($startAt -le (Get-Date)) {
            $startAt = $startAt.AddDays(1)
        }
        $trigger = $definition.Triggers.Create($TASK_TRIGGER_DAILY)
        $trigger.StartBoundary = $startAt.ToString("yyyy-MM-ddTHH:mm:ss")
        $trigger.DaysInterval = 1
        $trigger.Enabled = $true
        Write-Host "  trigger: daily $t"
    }

    # Action: keep executable, arguments, and working directory separate.
    # This avoids PowerShell 5.1 quoting issues with schtasks.exe /TR.
    $action = $definition.Actions.Create($TASK_ACTION_EXEC)
    $action.Path = $PythonExe
    $action.Arguments = "`"$ScriptPath`""
    $action.WorkingDirectory = $WorkDir

    $rootFolder.RegisterTaskDefinition(
        $TaskName,
        $definition,
        $TASK_CREATE_OR_UPDATE,
        $currentUser,
        $null,
        $TASK_LOGON_INTERACTIVE_TOKEN
    ) | Out-Null
}
catch {
    Write-Host "ERROR: task registration failed" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}


Write-Host ""
Write-Host "task '$TaskName' registered" -ForegroundColor Green
Write-Host "  triggers : $($schedTimes -join ', ')"
Write-Host "  wake     : enabled"
Write-Host ""
Write-Host "verify  : Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "run now : Start-ScheduledTask -TaskName '$TaskName'"
