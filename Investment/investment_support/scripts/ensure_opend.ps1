# Ensure moomoo OpenD (GUI edition) is running and its API port is open.
#
# Registered as the "Investment_OpenD" task (interactive logon only), because the
# GUI edition cannot start in the non-interactive session used by batch tasks.
#
# Exit codes: 0 = port open / 1 = executable not found / 2 = port still closed after waiting
#             (usually OpenD is waiting for a manual login)

param(
    [string]$ExePath = "$env:APPDATA\moomoo_OpenD\moomoo_OpenD.exe",
    [string]$HostName = "127.0.0.1",
    [int]$Port = 11111,
    [int]$WaitSec = 120
)

$LogDir = Join-Path (Split-Path $PSScriptRoot -Parent) "logs"
$LogFile = Join-Path $LogDir ("opend_{0}.log" -f (Get-Date -Format "yyyyMMdd"))

function Write-Log([string]$Level, [string]$Message) {
    $line = "{0} [{1}] ensure_opend: {2}{3}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message, [Environment]::NewLine
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
    [IO.File]::AppendAllText($LogFile, $line, (New-Object Text.UTF8Encoding($false)))
}

function Test-Port {
    $client = New-Object Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(2000)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Wait-Port {
    $deadline = (Get-Date).AddSeconds($WaitSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-Port) { return $true }
        Start-Sleep -Seconds 5
    }
    return $false
}

if (Test-Port) {
    exit 0
}

$running = Get-Process -Name "moomoo_OpenD" -ErrorAction SilentlyContinue
if ($running) {
    Write-Log "WARNING" "OpenD process is running (pid $($running[0].Id)) but port $Port is closed. Waiting up to $WaitSec s."
} else {
    if (-not (Test-Path $ExePath)) {
        Write-Log "ERROR" "OpenD executable not found: $ExePath"
        exit 1
    }
    Write-Log "INFO" "OpenD is not running. Starting $ExePath"
    Start-Process -FilePath $ExePath -WorkingDirectory (Split-Path $ExePath -Parent) -WindowStyle Minimized
}

if (Wait-Port) {
    Write-Log "INFO" "OpenD API port $Port is open."
    exit 0
}

Write-Log "ERROR" "OpenD API port $Port did not open within $WaitSec s. Manual login may be required (enable auto login in OpenD)."
exit 2
