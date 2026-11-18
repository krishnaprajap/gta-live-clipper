# Stops the clipper and everything it started (ffmpeg, streamlink).
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
New-Item -ItemType File -Force -Path (Join-Path $Root ".stop") | Out-Null
$pidFile = Join-Path $Root ".clipper.pid"
if (Test-Path $pidFile) {
    $id = (Get-Content $pidFile | Select-Object -First 1).Trim()
    taskkill /PID $id /T /F 2>$null | Out-Null
}
# give the supervisor time to notice the stop flag and exit
for ($i = 0; $i -lt 20 -and (Test-Path (Join-Path $Root ".supervisor.pid")); $i++) { Start-Sleep -Milliseconds 500 }
Write-Host "Clipper stopped."
