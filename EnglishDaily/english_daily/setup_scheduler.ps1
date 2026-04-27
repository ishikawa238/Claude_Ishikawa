# EnglishDaily - Task Scheduler Registration Script
# Run in PowerShell (as Administrator):
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\setup_scheduler.ps1

$PythonExe  = "C:\Users\bun22\Desktop\Ishikawa238\EnglishDaily\english_daily\.venv\Scripts\python.exe"
$ProjectDir = "C:\Users\bun22\Desktop\Ishikawa238\EnglishDaily\english_daily"

function Register-EnglishDailyTask {
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
Register-EnglishDailyTask `
    -TaskName "EnglishDaily_MorningBatch" `
    -Script   "-m scripts.batch_morning" `
    -Trigger  (New-ScheduledTaskTrigger -Daily -At "07:00") `
    -Desc     "EnglishDaily - Morning batch (task delivery + streak update)"

# Reminder - daily 17:30
Register-EnglishDailyTask `
    -TaskName "EnglishDaily_Reminder_1730" `
    -Script   "-m scripts.batch_reminder" `
    -Trigger  (New-ScheduledTaskTrigger -Daily -At "17:30") `
    -Desc     "EnglishDaily - Reminder (17:30)"

# Reminder - daily 19:30
Register-EnglishDailyTask `
    -TaskName "EnglishDaily_Reminder_1930" `
    -Script   "-m scripts.batch_reminder" `
    -Trigger  (New-ScheduledTaskTrigger -Daily -At "19:30") `
    -Desc     "EnglishDaily - Reminder (19:30)"

# Reminder - daily 21:30
Register-EnglishDailyTask `
    -TaskName "EnglishDaily_Reminder_2130" `
    -Script   "-m scripts.batch_reminder" `
    -Trigger  (New-ScheduledTaskTrigger -Daily -At "21:30") `
    -Desc     "EnglishDaily - Reminder (21:30)"

# Reminder - daily 23:30
Register-EnglishDailyTask `
    -TaskName "EnglishDaily_Reminder_2330" `
    -Script   "-m scripts.batch_reminder" `
    -Trigger  (New-ScheduledTaskTrigger -Daily -At "23:30") `
    -Desc     "EnglishDaily - Reminder (23:30)"

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  All tasks registered successfully." -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Schedule summary:"
Write-Host "  Daily 07:00  ->  EnglishDaily_MorningBatch"
Write-Host "  Daily 17:30  ->  EnglishDaily_Reminder_1730"
Write-Host "  Daily 19:30  ->  EnglishDaily_Reminder_1930"
Write-Host "  Daily 21:30  ->  EnglishDaily_Reminder_2130"
Write-Host "  Daily 23:30  ->  EnglishDaily_Reminder_2330"
Write-Host ""
Write-Host "To verify: run 'taskschd.msc'"
