# Investment Support - Task Scheduler Registration Script
# Run in PowerShell (as Administrator):
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\setup_scheduler.ps1

$PythonExe  = "C:\Users\bun22\AppData\Local\Python\pythoncore-3.14-64\python.exe"
$ProjectDir = "C:\Users\bun22\Desktop\Ishikawa238\Investment\investment_support"

function Register-InvestmentTask {
    param(
        [string]$TaskName,
        [string]$Script,
        [object]$Trigger,
        [string]$Desc,
        [switch]$NoTimeLimit
    )

    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed existing task: $TaskName"
    }

    $action = New-ScheduledTaskAction `
        -Execute $PythonExe `
        -Argument $Script `
        -WorkingDirectory $ProjectDir

    $timeLimit = if ($NoTimeLimit) { New-TimeSpan } else { New-TimeSpan -Hours 4 }

    $settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit $timeLimit `
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

# Evening batch (JP stocks) - daily 20:30
Register-InvestmentTask `
    -TaskName "Investment_EveningBatch_JP" `
    -Script   "-m scripts.batch_evening_jp" `
    -Trigger  (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday,Saturday,Sunday -At "18:30") `
    -Desc     "Investment Support - Evening batch (JP stocks)"

# Morning batch (all markets) - daily 07:30
Register-InvestmentTask `
    -TaskName "Investment_MorningBatch_ALL" `
    -Script   "-m scripts.batch_morning_all" `
    -Trigger  (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday,Saturday,Sunday -At "07:30") `
    -Desc     "Investment Support - Morning batch (all markets)"


# Dashboard (order_server.py) - start at logon
Register-InvestmentTask `
    -TaskName "Investment_Dashboard" `
    -Script   "-m scripts.order_server" `
    -Trigger  (New-ScheduledTaskTrigger -AtLogOn) `
    -Desc     "Investment Support - Dashboard (Flask on 127.0.0.1:5100)" `
    -NoTimeLimit

# Monthly batch - 1st of each month 08:00
# New-ScheduledTaskTrigger has no -Monthly parameter.
# Use schtasks.exe /create with monthly schedule instead of CIM trigger.
$monthlyTaskName = "Investment_MonthlyBatch"
if (Get-ScheduledTask -TaskName $monthlyTaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $monthlyTaskName -Confirm:$false
    Write-Host "Removed existing task: $monthlyTaskName"
}
schtasks.exe /create `
    /tn $monthlyTaskName `
    /tr "$PythonExe -m scripts.batch_monthly" `
    /sc monthly /d 1 /st 08:00 `
    /rl HIGHEST `
    /f | Out-Null
# Set working directory via CIM (schtasks /create doesn't support it directly)
$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "-m scripts.batch_monthly" `
    -WorkingDirectory $ProjectDir
Set-ScheduledTask -TaskName $monthlyTaskName -Action $action | Out-Null
Write-Host "Registered: $monthlyTaskName" -ForegroundColor Green

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  All tasks registered successfully." -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Schedule summary:"
Write-Host "  At logon     ->  Investment_Dashboard (127.0.0.1:5100)"
Write-Host "  Daily 07:30  ->  Investment_MorningBatch_ALL"
Write-Host "  Daily 18:30  ->  Investment_EveningBatch_JP"
Write-Host "  1st of month ->  Investment_MonthlyBatch"
Write-Host ""
Write-Host "To verify: run 'taskschd.msc'"
