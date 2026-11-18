<#
Stores your Twitch Client ID and Client Secret in .env (the secret is typed
hidden, validated with Twitch first, never printed), then runs a read-only
check of the whole Twitch integration and restarts the clipper.

  powershell -ExecutionPolicy Bypass -File scripts\windows\set-twitch-credentials.ps1
#>
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"
& $Py -m clipper credentials twitch
if ($LASTEXITCODE -ne 0) { exit 1 }
icacls (Join-Path $Root ".env") /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
& $Py -m clipper twitch-check --chat 15
& (Join-Path $PSScriptRoot "restart.ps1")
