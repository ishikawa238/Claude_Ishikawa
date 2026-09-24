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
    -Trigger  (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday,Saturday,Sunday -At "20:30") `
    -Desc     "Investment Support - Evening batch (JP stocks)"

# Morning batch (all markets) - daily 07:30
Register-InvestmentTask `
    -TaskName "Investment_MorningBatch_ALL" `
    -Script   "-m scripts.batch_morning_all" `
    -Trigger  (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday,Saturday,Sunday -At "07:30") `
    -Desc     "Investment Support - Morning batch (all markets)"

# Watchlist hourly monitor — every hour (batch skips when market closed)
$hourlyTaskName = "Investment_WatchlistHourly"
if (Get-ScheduledTask -TaskName $hourlyTaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $hourlyTaskName -Confirm:$false
    Write-Host "Removed existing task: $hourlyTaskName"
}
schtasks.exe /create `
    /tn $hourlyTaskName `
    /tr "$PythonExe -m scripts.batch_watchlist_hourly" `
    /sc hourly /mo 1 /st 09:00 `
    /rl HIGHEST `
    /f | Out-Null
$hourlyAction = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "-m scripts.batch_watchlist_hourly" `
    -WorkingDirectory $ProjectDir
Set-ScheduledTask -TaskName $hourlyTaskName -Action $hourlyAction | Out-Null
Write-Host "Registered: $hourlyTaskName" -ForegroundColor Green

# Dashboard (order_server.py) - start at logon
Register-InvestmentTask `
    -TaskName "Investment_Dashboard" `
    -Script   "-m scripts.order_server" `
    -Trigger  (New-ScheduledTaskTrigger -AtLogOn) `
    -Desc     "Investment Support - Dashboard (Flask on 127.0.0.1:5100)" `
    -NoTimeLimit

# Monthly batch - 1st of each month 08:00
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

# moomoo OpenD keeper - at logon + every hour at :10 (20 min before the 07:30 / 20:30 batches)
# The GUI edition of OpenD needs the interactive desktop, so this task runs only while the user is logged on.
$openDTaskName = "Investment_OpenD"
if (Get-ScheduledTask -TaskName $openDTaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $openDTaskName -Confirm:$false
    Write-Host "Removed existing task: $openDTaskName"
}
$openDAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ProjectDir\scripts\ensure_opend.ps1`"" `
    -WorkingDirectory $ProjectDir
$openDHourly = New-ScheduledTaskTrigger -Daily -At "00:10"
$openDHourly.Repetition = (New-ScheduledTaskTrigger -Once -At "00:10" `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Hours 23 -Minutes 59)).Repetition
$openDLogon = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$openDSettings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew
$openDPrincipal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited
Register-ScheduledTask `
    -TaskName    $openDTaskName `
    -Description "Investment Support - Keep moomoo OpenD running (at logon + hourly at :10)" `
    -Action      $openDAction `
    -Trigger     @($openDLogon, $openDHourly) `
    -Settings    $openDSettings `
    -Principal   $openDPrincipal `
    -Force | Out-Null
Write-Host "Registered: $openDTaskName" -ForegroundColor Green

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  All tasks registered successfully." -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Schedule summary:"
Write-Host "  At logon     ->  Investment_Dashboard (127.0.0.1:5100)"
Write-Host "  Logon+hourly ->  Investment_OpenD (starts moomoo OpenD if port 11111 is closed)"
Write-Host "  Daily 07:30  ->  Investment_MorningBatch_ALL"
Write-Host "  Daily 20:30  ->  Investment_EveningBatch_JP"
Write-Host "  Hourly       ->  Investment_WatchlistHourly (market-hours gate inside batch)"
Write-Host "  1st of month ->  Investment_MonthlyBatch"
Write-Host ""
Write-Host "To verify: run 'taskschd.msc'"
