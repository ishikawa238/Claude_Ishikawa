# Morning English プレイリスト更新 - タスクスケジューラ登録スクリプト
#
# 管理者権限の PowerShell で実行してください:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\setup_scheduler.ps1

$TaskName = "MorningEnglishPlaylistUpdate"
$ProjectDir = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $ProjectDir "morning_playlist\venv\Scripts\python.exe"
$Script = Join-Path $ProjectDir "morning_playlist\scripts\update_playlist.py"
$WorkDir = Join-Path $ProjectDir "morning_playlist"

# 既存タスクがあれば削除
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "既存タスク '$TaskName' を削除しました"
}

# トリガー: 毎日 7:00
$Trigger = New-ScheduledTaskTrigger -Daily -At 7:00AM

# アクション: Python スクリプト実行
$Action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "`"$Script`"" `
    -WorkingDirectory $WorkDir

# 設定: スリープ解除、AC/バッテリー両対応
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -WakeToRun `
    -StartWhenAvailable

# 登録
Register-ScheduledTask `
    -TaskName $TaskName `
    -Trigger $Trigger `
    -Action $Action `
    -Settings $Settings `
    -Description "Morning English: Spotify プレイリスト自動更新 & 再生" `
    -RunLevel Limited

Write-Host ""
Write-Host "タスク '$TaskName' を登録しました"
Write-Host "  実行時刻: 毎日 7:00"
Write-Host "  Python: $PythonExe"
Write-Host "  スクリプト: $Script"
Write-Host ""
Write-Host "確認: Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "手動実行: Start-ScheduledTask -TaskName '$TaskName'"
