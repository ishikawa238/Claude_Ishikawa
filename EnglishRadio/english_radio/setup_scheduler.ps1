# EnglishRadio - タスクスケジューラ登録スクリプト
#
# 初回は管理者権限の PowerShell で実行してください:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\setup_scheduler.ps1
#
# 登録するタスク:
#   EnglishRadio_MorningPlay : config.json の radio.schedules の各開始時刻に朝のラジオを再生
#   EnglishRadio_WebServer   : ログオン時に Web 管理画面 (http://127.0.0.1:5210/) を起動
#   EnglishRadio_StudyBatch  : 毎日 09:00 に学習バッチ（文字起こし・和訳・単語）を実行（スリープ解除なし）
#
# -MorningOnly を付けると朝のタスクだけを更新する（Web 管理画面のスケジュール保存から呼ばれる）。

param(
    [switch]$MorningOnly
)

$MorningTask = "EnglishRadio_MorningPlay"
$WebTask = "EnglishRadio_WebServer"
$StudyTask = "EnglishRadio_StudyBatch"
$StudyTime = "09:00"
$PythonExe = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
$PythonwExe = Join-Path $PSScriptRoot "venv\Scripts\pythonw.exe"
$MorningScript = Join-Path $PSScriptRoot "scripts\morning_radio.py"
$WebScript = Join-Path $PSScriptRoot "scripts\web_server.py"
$StudyScript = Join-Path $PSScriptRoot "scripts\study_batch.py"
$WorkDir = $PSScriptRoot

foreach ($p in @($PythonExe, $MorningScript)) {
    if (-not (Test-Path $p)) {
        Write-Host "ERROR: not found: $p" -ForegroundColor Red
        exit 1
    }
}

# Task Scheduler COM API constants
$TASK_ACTION_EXEC = 0
$TASK_TRIGGER_DAILY = 2
$TASK_TRIGGER_WEEKLY = 3
$TASK_TRIGGER_LOGON = 9
$TASK_CREATE_OR_UPDATE = 6
$TASK_LOGON_INTERACTIVE_TOKEN = 3
$TASK_RUNLEVEL_LUA = 0

# 曜日ビットマスク（日=1, 月=2, 火=4, 水=8, 木=16, 金=32, 土=64）
$WEEKDAYS_MASK = 2 + 4 + 8 + 16 + 32
$WEEKENDS_MASK = 1 + 64

$service = New-Object -ComObject "Schedule.Service"
$service.Connect()
$rootFolder = $service.GetFolder("\")
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

function New-BaseDefinition([string]$description, [string]$timeLimit) {
    $def = $service.NewTask(0)
    $def.RegistrationInfo.Description = $description
    $def.Principal.UserId = $currentUser
    $def.Principal.LogonType = $TASK_LOGON_INTERACTIVE_TOKEN
    $def.Principal.RunLevel = $TASK_RUNLEVEL_LUA
    $def.Settings.Enabled = $true
    $def.Settings.StartWhenAvailable = $true
    $def.Settings.DisallowStartIfOnBatteries = $false
    $def.Settings.StopIfGoingOnBatteries = $false
    $def.Settings.ExecutionTimeLimit = $timeLimit
    return $def
}

function Add-ExecAction($def, [string]$exe, [string]$script) {
    $action = $def.Actions.Create($TASK_ACTION_EXEC)
    $action.Path = $exe
    $action.Arguments = "`"$script`""
    $action.WorkingDirectory = $WorkDir
}

function Get-Schedules {
    $configPath = Join-Path $PSScriptRoot "config.json"
    if (Test-Path $configPath) {
        $cfg = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($cfg.radio.schedules) { return @($cfg.radio.schedules) }
    }
    return @([pscustomobject]@{ start = "07:00"; days = "daily" })
}

function Add-ScheduleTrigger($def, $sched) {
    $parts = $sched.start -split ":"
    $startAt = (Get-Date).Date.AddHours([int]$parts[0]).AddMinutes([int]$parts[1])
    if ($startAt -le (Get-Date)) { $startAt = $startAt.AddDays(1) }

    $days = if ($sched.days) { $sched.days } else { "daily" }
    if ($days -eq "daily") {
        $trigger = $def.Triggers.Create($TASK_TRIGGER_DAILY)
        $trigger.DaysInterval = 1
    }
    else {
        $trigger = $def.Triggers.Create($TASK_TRIGGER_WEEKLY)
        $trigger.WeeksInterval = 1
        $trigger.DaysOfWeek = if ($days -eq "weekdays") { $WEEKDAYS_MASK } else { $WEEKENDS_MASK }
    }
    $trigger.StartBoundary = $startAt.ToString("yyyy-MM-ddTHH:mm:ss")
    $trigger.Enabled = $true
    Write-Host "  trigger: $($sched.start) ($days)"
}

function Register-Definition([string]$name, $def) {
    $rootFolder.RegisterTaskDefinition(
        $name, $def, $TASK_CREATE_OR_UPDATE, $currentUser, $null, $TASK_LOGON_INTERACTIVE_TOKEN
    ) | Out-Null
    Write-Host "task '$name' registered" -ForegroundColor Green
}

try {
    # --- 朝のラジオ ---
    $morning = New-BaseDefinition "EnglishRadio: download podcast RSS and play with VLC" "PT1H"
    $morning.Settings.WakeToRun = $true
    foreach ($s in Get-Schedules) { Add-ScheduleTrigger $morning $s }
    Add-ExecAction $morning $PythonExe $MorningScript
    Register-Definition $MorningTask $morning

    # --- Web 管理画面（ログオン時に起動、時間制限なし） ---
    if (-not $MorningOnly) {
        $web = New-BaseDefinition "EnglishRadio: web dashboard on http://127.0.0.1:5210/" "PT0S"
        $logon = $web.Triggers.Create($TASK_TRIGGER_LOGON)
        $logon.UserId = $currentUser
        $logon.Enabled = $true
        $exe = if (Test-Path $PythonwExe) { $PythonwExe } else { $PythonExe }
        Add-ExecAction $web $exe $WebScript
        Register-Definition $WebTask $web

        # --- 学習バッチ（朝の再生のあと。文字起こしは長くかかるので 3 時間まで） ---
        $study = New-BaseDefinition "EnglishRadio: transcribe episodes and build study notes" "PT3H"
        Add-ScheduleTrigger $study ([pscustomobject]@{ start = $StudyTime; days = "daily" })
        Add-ExecAction $study $PythonExe $StudyScript
        Register-Definition $StudyTask $study
    }
}
catch {
    Write-Host "ERROR: task registration failed" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "verify  : Get-ScheduledTask -TaskName '$MorningTask'"
Write-Host "run now : Start-ScheduledTask -TaskName '$MorningTask'"
if (-not $MorningOnly) {
    Write-Host "web     : Start-ScheduledTask -TaskName '$WebTask'  ->  http://127.0.0.1:5210/"
    Write-Host "study   : Start-ScheduledTask -TaskName '$StudyTask'"
}
