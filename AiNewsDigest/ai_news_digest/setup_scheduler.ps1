# AiNewsDigest - Task Scheduler Registration Script
# Run in PowerShell (as Administrator):
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\setup_scheduler.ps1

$PythonExe  = "C:\Users\bun22\Desktop\Ishikawa238\AiNewsDigest\ai_news_digest\.venv\Scripts\python.exe"
$ProjectDir = "C:\Users\bun22\Desktop\Ishikawa238\AiNewsDigest\ai_news_digest"

function Register-NewsDigestTask {
    param(
        [string]$TaskName,
        [string]$Script,
        [object]$Trigger,
        [string]$Desc
    )

    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed existing task: $TaskName"
    }

    $action = New-ScheduledTaskAction `
        -Execute $PythonExe `
        -Argument $Script `
        -WorkingDirectory $ProjectDir

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
        -TaskName    $TaskName `
        -Description $Desc `
        -Action      $action `
        -Trigger     $Trigger `
        -Settings    $settings `
        -Principal   $principal `
        -Force | Out-Null

    Write-Host "Registered: $TaskName" -ForegroundColor Green
}

# Morning batch - daily 07:00
Register-NewsDigestTask `
    -TaskName "AiNewsDigest_MorningBatch" `
    -Script   "-m scripts.batch_morning" `
    -Trigger  (New-ScheduledTaskTrigger -Daily -At "07:00") `
    -Desc     "AiNewsDigest - Morning batch (RSS fetch + Slack notify)"

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Task registered successfully." -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Schedule summary:"
Write-Host "  Daily 07:00  ->  AiNewsDigest_MorningBatch"
Write-Host ""
Write-Host "To verify: run 'taskschd.msc'"
