<#
GTA VI Clipper - one-time Windows setup (Windows 10 21H2+ / Windows 11, x64).

  powershell -ExecutionPolicy Bypass -File scripts\windows\setup.ps1 [-DataDir D:\GTAClipper\data]
                                                                    [-NoAutostart] [-SkipSelfTest]

Installs (per-user, via winget) Python 3.12, FFmpeg and the VC++ runtime if
missing; creates .venv with Streamlink + Faster-Whisper; writes config and
.env (random dashboard password, file readable only by you); downloads the
Whisper model into DataDir; runs an offline self-test; registers autostart.
Re-running is safe: existing config/.env are kept.
#>
param(
    [string]$DataDir = "C:\GTAClipper\data",
    [switch]$NoAutostart,
    [switch]$SkipSelfTest
)
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root
function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User") + ";" +
                (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links")
}
function Winget-Install($id) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "winget not found. Install 'App Installer' from the Microsoft Store, or install $id manually, then re-run."
    }
    winget install -e --id $id --scope user --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {   # some packages only offer machine scope
        winget install -e --id $id --silent --accept-package-agreements --accept-source-agreements
    }
    Refresh-Path
}

# ---------------------------------------------------------------- Python
Step "Python 3.12"
function Find-Python {
    $cands = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        try {
            $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
            $p = (& py -3.12 -c "import sys;print(sys.executable)" 2>$null)
            if ($LASTEXITCODE -eq 0 -and $p) { $cands += ($p | Select-Object -Last 1).Trim() }
        } catch { } finally { $ErrorActionPreference = $old }
    }
    $cands += (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe")
    $cands += "C:\Program Files\Python312\python.exe"
    foreach ($c in $cands) { if ($c -and (Test-Path $c)) { return $c } }
    return $null
}
$Python = Find-Python
if (-not $Python) { Winget-Install "Python.Python.3.12"; $Python = Find-Python }
if (-not $Python) { throw "Python 3.12 not found after install. Install it from python.org and re-run." }
Write-Host "Python: $Python"

# ---------------------------------------------------------------- FFmpeg
Step "FFmpeg"
function Find-Exe($name) {
    $c = Get-Command $name -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $hit = Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages") -Filter "$name.exe" -Recurse -ErrorAction SilentlyContinue |
           Where-Object { $_.FullName -like "*Gyan.FFmpeg*" } | Select-Object -First 1
    if ($hit) { return $hit.FullName }
    return $null
}
$Ffmpeg = Find-Exe "ffmpeg"
if (-not $Ffmpeg) { Winget-Install "Gyan.FFmpeg"; $Ffmpeg = Find-Exe "ffmpeg" }
$Ffprobe = Find-Exe "ffprobe"
if (-not $Ffmpeg -or -not $Ffprobe) { throw "FFmpeg not found after install. Install a full build from gyan.dev and add its bin folder to PATH." }
$old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
$filters = (& $Ffmpeg -hide_banner -filters 2>$null) -join "`n"
$ErrorActionPreference = $old
foreach ($f in @(" subtitles ", " drawtext ", " scdet ", " astats ")) {
    if ($filters -notmatch [regex]::Escape($f)) { throw "Your FFmpeg build lacks the$f filter. Install Gyan.FFmpeg (full build)." }
}
Write-Host "FFmpeg: $Ffmpeg"

Step "Microsoft Visual C++ runtime (needed by Faster-Whisper)"
if (-not (Test-Path "$env:WINDIR\System32\vcruntime140_1.dll")) { Winget-Install "Microsoft.VCRedist.2015+.x64" }
else { Write-Host "already installed" }

# ---------------------------------------------------------------- venv + packages
Step "Python environment (.venv) with Streamlink and Faster-Whisper"
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) { & $Python -m venv (Join-Path $Root ".venv") }
& $VenvPy -m pip install --disable-pip-version-check -q --upgrade pip
& $VenvPy -m pip install --disable-pip-version-check -q -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# ---------------------------------------------------------------- data dir, config, .env
Step "Data folder and configuration"
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$DataDirFwd = (Resolve-Path $DataDir).Path -replace "\\", "/"
$Cfg = Join-Path $Root "config\config.yaml"
$ramGb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
$model = if ($ramGb -lt 8) { "base" } else { "small" }
if (-not (Test-Path $Cfg)) {
    $yaml = Get-Content (Join-Path $Root "config\config.windows.yaml") -Raw
    $yaml = $yaml -replace "(?m)^data_dir:.*$", "data_dir: '$DataDirFwd'"
    $yaml = $yaml -replace "(?m)^  model: small(.*)$", "  model: $model`$1"
    Set-Content -Path $Cfg -Value $yaml -Encoding UTF8
    Write-Host "created config\config.yaml (data_dir=$DataDirFwd, whisper=$model, RAM=$ramGb GB)"
} else { Write-Host "kept existing config\config.yaml" }

$EnvFile = Join-Path $Root ".env"
if (-not (Test-Path $EnvFile)) {
    Copy-Item (Join-Path $Root ".env.example") $EnvFile
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    (Get-Content $EnvFile) -replace "^DASHBOARD_TOKEN=.*$", "DASHBOARD_TOKEN=$token" | Set-Content $EnvFile -Encoding UTF8
    Remove-Variable token
    Write-Host "created .env with a random dashboard password"
}
# non-secret machine settings (re-written on every run)
$lines = Get-Content $EnvFile | Where-Object { $_ -notmatch "^(FFMPEG_BINARY|FFPROBE_BINARY|HF_HOME)=" }
$lines += "FFMPEG_BINARY=$Ffmpeg"
$lines += "FFPROBE_BINARY=$Ffprobe"
$lines += "HF_HOME=$DataDirFwd/hf"
Set-Content -Path $EnvFile -Value $lines -Encoding UTF8
# only the current user may read .env
icacls $EnvFile /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null

# ---------------------------------------------------------------- Whisper model
Step "Downloading Whisper model '$model' (one time, stored in $DataDirFwd/hf)"
$env:HF_HOME = "$DataDirFwd/hf"
& $VenvPy -c "from faster_whisper import WhisperModel; WhisperModel('$model', device='cpu', compute_type='int8'); print('model ready')"
if ($LASTEXITCODE -ne 0) { Write-Warning "Whisper model download failed; clips will be made without captions until it succeeds." }

# ---------------------------------------------------------------- checks
Step "Pre-flight check"
& $VenvPy -m clipper doctor
if ($LASTEXITCODE -ne 0) { throw "doctor reported problems (see above)" }

if (-not $SkipSelfTest) {
    Step "Offline self-test (about 2 minutes: synthetic stream -> clip -> vertical video -> outbox)"
    & $VenvPy (Join-Path $Root "scripts\make_test_stream.py") --out "$DataDirFwd/test/stream.mp4"
    & $VenvPy -m clipper run --file "$DataDirFwd/test/stream.mp4" --chat "$DataDirFwd/test/chat.jsonl" --channel SelfTest
    if ($LASTEXITCODE -ne 0) { throw "self-test failed" }
    $out = Get-ChildItem (Join-Path $DataDir "outbox") -Filter "*SelfTest*.mp4" -ErrorAction SilentlyContinue
    if (-not $out) { throw "self-test produced no clip in $DataDir\outbox" }
    Write-Host "self-test OK: $($out[-1].FullName)" -ForegroundColor Green
}

# ---------------------------------------------------------------- autostart
if (-not $NoAutostart) {
    Step "Autostart at Windows logon (Task Scheduler, current user, no admin needed)"
    & (Join-Path $PSScriptRoot "autostart.ps1") -Enable
}

Write-Host "`nDone." -ForegroundColor Green
Write-Host "Dashboard : http://127.0.0.1:8080   (user: anything, password: DASHBOARD_TOKEN in .env)"
Write-Host "Show password : Select-String -Path `"$EnvFile`" -Pattern '^DASHBOARD_TOKEN='"
Write-Host "Next: edit config\config.yaml (Twitch login, authorized: true) and add TWITCH_CLIENT_ID/SECRET to .env,"
Write-Host "      then run scripts\windows\restart.ps1"
