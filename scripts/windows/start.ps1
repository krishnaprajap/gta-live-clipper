<#
Runs the clipper (dashboard + autonomous capture) and restarts it if it
crashes. Started by Task Scheduler at logon, or manually:
  powershell -ExecutionPolicy Bypass -File scripts\windows\start.ps1
Stop with scripts\windows\stop.ps1. Logs: <data_dir>\logs\
#>
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
$env:PYTHONUTF8 = "1"
if (-not (Test-Path $VenvPy)) { throw "Run scripts\windows\setup.ps1 first." }

# single instance
$created = $false
$mutex = New-Object System.Threading.Mutex($true, "Local\GTAClipperSupervisor", [ref]$created)
if (-not $created) { Write-Host "Clipper is already running."; exit 0 }

$DataDir = (& $VenvPy -c "from clipper.config import load_config; print(load_config().data_dir)").Trim()
$LogDir = Join-Path $DataDir "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$StopFlag = Join-Path $Root ".stop"
Remove-Item $StopFlag -ErrorAction SilentlyContinue
Set-Content -Path (Join-Path $Root ".supervisor.pid") -Value $PID

$delay = 10
while (-not (Test-Path $StopFlag)) {
    $started = Get-Date
    $p = Start-Process -FilePath $VenvPy -ArgumentList @("-m", "clipper", "serve", "--log-file", "`"$LogDir\clipper.log`"") `
         -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
         -RedirectStandardError (Join-Path $LogDir "stderr.log") -RedirectStandardOutput (Join-Path $LogDir "stdout.log")
    Set-Content -Path (Join-Path $Root ".clipper.pid") -Value $p.Id
    $p.WaitForExit()
    if (Test-Path $StopFlag) { break }
    Copy-Item (Join-Path $LogDir "stderr.log") (Join-Path $LogDir "stderr.prev.log") -ErrorAction SilentlyContinue
    $ran = ((Get-Date) - $started).TotalSeconds
    $delay = if ($ran -gt 300) { 10 } else { [math]::Min($delay * 2, 300) }
    Add-Content (Join-Path $LogDir "supervisor.log") "$(Get-Date -Format s) exit code $($p.ExitCode) after $([int]$ran)s; restarting in $delay s"
    Start-Sleep -Seconds $delay
}
Remove-Item $StopFlag, (Join-Path $Root ".clipper.pid"), (Join-Path $Root ".supervisor.pid") -ErrorAction SilentlyContinue
$mutex.ReleaseMutex()
