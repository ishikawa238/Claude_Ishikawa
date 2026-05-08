# Ishikawa238 - 全システム稼働状態チェック
#
# PowerShell で実行（管理者権限不要）:
#   .\status.ps1
#
# 全タスクスケジューラの登録状態・最終実行結果と、
# 常駐サーバーの応答を一覧表示する。

# ---------------------------------------------------------------------------
# 監視対象タスク一覧
# ---------------------------------------------------------------------------
$tasks = @(
    @{ Name = "Investment_Dashboard";            Group = "Investment" },
    @{ Name = "Investment_MorningBatch_ALL";     Group = "Investment" },
    @{ Name = "Investment_EveningBatch_JP";      Group = "Investment" },
    @{ Name = "Investment_MonthlyBatch";         Group = "Investment" },
    @{ Name = "MorningEnglishPlaylistUpdate";    Group = "MorningPlaylist" },
    @{ Name = "FreelanceScout_MorningBatch";     Group = "FreelanceScout" },
    @{ Name = "AiNewsDigest_MorningBatch";       Group = "AiNewsDigest" },
    @{ Name = "EnglishDaily_MorningBatch";       Group = "EnglishDaily" },
    @{ Name = "EnglishDaily_Reminder_1730";      Group = "EnglishDaily" },
    @{ Name = "EnglishDaily_Reminder_1930";      Group = "EnglishDaily" },
    @{ Name = "EnglishDaily_Reminder_2130";      Group = "EnglishDaily" },
    @{ Name = "EnglishDaily_Reminder_2330";      Group = "EnglishDaily" },
    @{ Name = "PaperWatch_MorningBatch";         Group = "PaperWatch" },
    @{ Name = "Ishikawa238_Watchdog";            Group = "Watchdog" },
    @{ Name = "Ishikawa238_AutoCommit";          Group = "AutoCommit" }
)

# ---------------------------------------------------------------------------
# 常駐サーバー一覧
# ---------------------------------------------------------------------------
$servers = @(
    @{ Label = "Investment Dashboard"; Url = "http://127.0.0.1:5100/health" }
)

# ---------------------------------------------------------------------------
# 表示
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Ishikawa238 - System Status" -ForegroundColor Cyan
Write-Host "  $(Get-Date -Format 'yyyy-MM-dd HH:mm')" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

$registered = 0
$missing    = 0
$prevGroup  = ""

foreach ($entry in $tasks) {
    $name  = $entry.Name
    $group = $entry.Group

    # グループ見出し
    if ($group -ne $prevGroup) {
        if ($prevGroup -ne "") { Write-Host "" }
        Write-Host "--- $group ---" -ForegroundColor White
        $prevGroup = $group
    }

    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($task) {
        $info       = $task | Get-ScheduledTaskInfo
        $lastResult = $info.LastTaskResult

        # 最終実行時刻
        $lastRun = "---"
        if ($info.LastRunTime -and $info.LastRunTime.Year -gt 1999) {
            $lastRun = $info.LastRunTime.ToString("yyyy-MM-dd HH:mm")
        }

        # 結果アイコン
        $icon  = "[??]"
        $color = "Gray"
        switch ($lastResult) {
            0       { $icon = "[OK]";  $color = "Green"  }   # 正常終了
            267009  { $icon = "[RUN]"; $color = "Cyan"   }   # 実行中
            267014  { $icon = "[--]";  $color = "Gray"   }   # 未実行
            default { $icon = "[NG]";  $color = "Yellow" }   # エラー
        }

        # Running 状態の場合もアイコンを上書き
        if ($task.State -eq "Running") {
            $icon  = "[RUN]"
            $color = "Cyan"
        }

        Write-Host ("  {0,-40} {1,5}  {2}" -f $name, $icon, $lastRun) -ForegroundColor $color
        $registered++
    }
    else {
        Write-Host ("  {0,-40} {1}" -f $name, "[NOT REGISTERED]") -ForegroundColor Red
        $missing++
    }
}

# ---------------------------------------------------------------------------
# 常駐サーバーの応答チェック
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "--- Servers ---" -ForegroundColor White

foreach ($srv in $servers) {
    try {
        $null = Invoke-WebRequest -Uri $srv.Url -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
        Write-Host ("  {0,-40} {1}" -f $srv.Label, "[OK] running") -ForegroundColor Green
    }
    catch {
        Write-Host ("  {0,-40} {1}" -f $srv.Label, "[NG] not responding") -ForegroundColor Red
    }
}

# ---------------------------------------------------------------------------
# サマリー
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Tasks: $registered registered, $missing missing" -ForegroundColor $(
    if ($missing -eq 0) { "Green" } else { "Yellow" }
)
if ($missing -gt 0) {
    Write-Host "  -> Run '.\setup_all.ps1' (admin) to register" -ForegroundColor Yellow
}
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
