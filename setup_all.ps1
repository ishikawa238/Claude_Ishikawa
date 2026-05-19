# Ishikawa238 - 全システム タスクスケジューラ一括登録
#
# 管理者権限の PowerShell で実行:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\setup_all.ps1
#
# 各サブシステムの setup_scheduler.ps1 を順次実行し、
# watchdog のタスクも登録する。

$RepoRoot = $PSScriptRoot

# ---------------------------------------------------------------------------
# サブシステムの setup_scheduler.ps1 一覧
# ---------------------------------------------------------------------------
$schedulerScripts = @(
    @{ Label = "Investment";      Path = "Investment\investment_support\setup_scheduler.ps1" },
    @{ Label = "MorningPlaylist"; Path = "MorningPlaylist\morning_playlist\setup_scheduler.ps1" },
    @{ Label = "FreelanceScout";  Path = "FreelanceScout\freelance_scout\setup_scheduler.ps1" },
    @{ Label = "AiNewsDigest";    Path = "AiNewsDigest\ai_news_digest\setup_scheduler.ps1" },
    @{ Label = "EnglishDaily";    Path = "EnglishDaily\english_daily\setup_scheduler.ps1" },
    @{ Label = "PaperWatch";      Path = "PaperWatch\paper_watch\setup_scheduler.ps1" }
)

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Ishikawa238 - All Systems Setup" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

$succeeded = 0
$failed    = 0
$skipped   = 0

foreach ($entry in $schedulerScripts) {
    $fullPath = Join-Path $RepoRoot $entry.Path
    $label    = $entry.Label

    if (-not (Test-Path $fullPath)) {
        Write-Host "SKIP: $label - script not found ($fullPath)" -ForegroundColor Yellow
        $skipped++
        Write-Host ""
        continue
    }

    Write-Host "=== $label ===" -ForegroundColor White
    try {
        & $fullPath
        $succeeded++
    }
    catch {
        Write-Host "FAILED: $label - $($_.Exception.Message)" -ForegroundColor Red
        $failed++
    }
    Write-Host ""
}

# ---------------------------------------------------------------------------
# Watchdog タスク登録（日次 22:00 — 全バッチ終了後に監視）
# ---------------------------------------------------------------------------
Write-Host "=== Watchdog ===" -ForegroundColor White

$watchdogTaskName = "Ishikawa238_Watchdog"
$watchdogPython   = Join-Path $RepoRoot "watchdog\.venv\Scripts\python.exe"
$watchdogDir      = Join-Path $RepoRoot "watchdog"

if (-not (Test-Path $watchdogPython)) {
    Write-Host "SKIP: Watchdog - python.exe not found ($watchdogPython)" -ForegroundColor Yellow
    Write-Host "  watchdog/.venv を作成してください" -ForegroundColor Yellow
    $skipped++
} else {
    if (Get-ScheduledTask -TaskName $watchdogTaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $watchdogTaskName -Confirm:$false
        Write-Host "Removed existing task: $watchdogTaskName"
    }

    $action = New-ScheduledTaskAction `
        -Execute $watchdogPython `
        -Argument "run_watchdog.py" `
        -WorkingDirectory $watchdogDir

    $trigger = New-ScheduledTaskTrigger -Daily -At "22:00"

    $settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
        -RunOnlyIfNetworkAvailable `
        -StartWhenAvailable `
        -DontStopIfGoingOnBatteries

    $principal = New-ScheduledTaskPrincipal `
        -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType S4U `
        -RunLevel Highest

    Register-ScheduledTask `
        -TaskName    $watchdogTaskName `
        -Description "Ishikawa238 - Watchdog (cross-system health monitor, daily 22:00)" `
        -Action      $action `
        -Trigger     $trigger `
        -Settings    $settings `
        -Principal   $principal `
        -Force | Out-Null

    Write-Host "Registered: $watchdogTaskName (daily 22:00)" -ForegroundColor Green
    $succeeded++
}

# ---------------------------------------------------------------------------
# Watchdog Dashboard タスク登録（ログオン時 — 常駐 Web サーバー）
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Watchdog Dashboard ===" -ForegroundColor White

$dashboardTaskName = "Ishikawa238_WatchdogDashboard"
$dashboardPython   = Join-Path $RepoRoot "watchdog\.venv\Scripts\python.exe"
$dashboardDir      = Join-Path $RepoRoot "watchdog"

if (-not (Test-Path $dashboardPython)) {
    Write-Host "SKIP: Watchdog Dashboard - python.exe not found ($dashboardPython)" -ForegroundColor Yellow
    Write-Host "  watchdog/.venv を作成してください" -ForegroundColor Yellow
    $skipped++
} else {
    if (Get-ScheduledTask -TaskName $dashboardTaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $dashboardTaskName -Confirm:$false
        Write-Host "Removed existing task: $dashboardTaskName"
    }

    $action = New-ScheduledTaskAction `
        -Execute $dashboardPython `
        -Argument "run_dashboard.py" `
        -WorkingDirectory $dashboardDir

    $trigger = New-ScheduledTaskTrigger -AtLogOn

    $settings = New-ScheduledTaskSettingsSet `
        -RunOnlyIfNetworkAvailable `
        -DontStopIfGoingOnBatteries `
        -AllowStartIfOnBatteries
    $settings.ExecutionTimeLimit = "PT0S"

    $principal = New-ScheduledTaskPrincipal `
        -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType S4U `
        -RunLevel Highest

    Register-ScheduledTask `
        -TaskName    $dashboardTaskName `
        -Description "Ishikawa238 - Watchdog Dashboard (system status web UI, port 5200)" `
        -Action      $action `
        -Trigger     $trigger `
        -Settings    $settings `
        -Principal   $principal `
        -Force | Out-Null

    Write-Host "Registered: $dashboardTaskName (at logon, persistent)" -ForegroundColor Green
    $succeeded++
}

# ---------------------------------------------------------------------------
# AutoCommit タスク登録（日次 03:00 — 変更自動コミット＆プッシュ）
# ---------------------------------------------------------------------------
Write-Host "=== AutoCommit ===" -ForegroundColor White

$autoCommitTaskName = "Ishikawa238_AutoCommit"
$autoCommitPython   = Join-Path $RepoRoot "auto_commit\.venv\Scripts\python.exe"
$autoCommitDir      = Join-Path $RepoRoot "auto_commit"

if (-not (Test-Path $autoCommitPython)) {
    Write-Host "SKIP: AutoCommit - python.exe not found ($autoCommitPython)" -ForegroundColor Yellow
    Write-Host "  auto_commit/.venv を作成してください" -ForegroundColor Yellow
    $skipped++
} else {
    if (Get-ScheduledTask -TaskName $autoCommitTaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $autoCommitTaskName -Confirm:$false
        Write-Host "Removed existing task: $autoCommitTaskName"
    }

    $action = New-ScheduledTaskAction `
        -Execute $autoCommitPython `
        -Argument "run_auto_commit.py" `
        -WorkingDirectory $autoCommitDir

    $trigger = New-ScheduledTaskTrigger -Daily -At "03:00"

    $settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
        -RunOnlyIfNetworkAvailable `
        -StartWhenAvailable `
        -DontStopIfGoingOnBatteries

    $principal = New-ScheduledTaskPrincipal `
        -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType S4U `
        -RunLevel Highest

    Register-ScheduledTask `
        -TaskName    $autoCommitTaskName `
        -Description "Ishikawa238 - AutoCommit (daily 03:00, auto commit & push changes)" `
        -Action      $action `
        -Trigger     $trigger `
        -Settings    $settings `
        -Principal   $principal `
        -Force | Out-Null

    Write-Host "Registered: $autoCommitTaskName (daily 03:00)" -ForegroundColor Green
    $succeeded++
}

# ---------------------------------------------------------------------------
# サマリー
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Setup complete" -ForegroundColor Cyan
Write-Host "    Succeeded : $succeeded" -ForegroundColor Green
if ($failed -gt 0) {
    Write-Host "    Failed    : $failed" -ForegroundColor Red
}
if ($skipped -gt 0) {
    Write-Host "    Skipped   : $skipped" -ForegroundColor Yellow
}
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Verify : taskschd.msc or .\status.ps1"
Write-Host ""
