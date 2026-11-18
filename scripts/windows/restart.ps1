# Restart after changing config\config.yaml or .env
& (Join-Path $PSScriptRoot "stop.ps1")
if (Get-ScheduledTask -TaskName "GTA Clipper" -ErrorAction SilentlyContinue) {
    Start-ScheduledTask -TaskName "GTA Clipper"
    Write-Host "Started via Task Scheduler."
} else {
    Start-Process powershell.exe -WindowStyle Hidden -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$(Join-Path $PSScriptRoot 'start.ps1')`"")
    Write-Host "Started in the background."
}
Write-Host "Dashboard: http://127.0.0.1:8080"
