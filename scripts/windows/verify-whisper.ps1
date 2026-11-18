<#
Diagnoses/repairs the Whisper model and proves that captions really work:
 1. whisper status -> repair (resume) if anything is incomplete
 2. Windows' built-in voice speaks a known sentence -> speech.wav
 3. Whisper transcribes speech.wav and must match the sentence
 4. the speech is mixed into the existing test video, the full clipping
    pipeline runs on it, and the resulting clip must have burned-in captions

  powershell -ExecutionPolicy Bypass -File scripts\windows\verify-whisper.ps1 [-Clean]
  -Clean : delete the model and download it from scratch instead of resuming
#>
param([switch]$Clean)
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { throw "Run scripts\windows\setup.ps1 first." }
$DataDir = (& $Py -c "from clipper.config import load_config; print(load_config().data_dir)").Trim()
$TestDir = Join-Path $DataDir "test"
New-Item -ItemType Directory -Force -Path $TestDir | Out-Null

Write-Host "`n==> 0. Pinned Whisper dependencies (faster-whisper / PyAV / CTranslate2)" -ForegroundColor Cyan
& $Py -m pip install --disable-pip-version-check -q -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install -r requirements.txt failed" }
& $Py -c "import av, faster_whisper, ctranslate2; print('av', av.__version__, '| faster-whisper', faster_whisper.__version__, '| ctranslate2', ctranslate2.__version__)"

Write-Host "`n==> 1. Model status" -ForegroundColor Cyan
& $Py -m clipper whisper status
if ($Clean -or $LASTEXITCODE -ne 0) {
    Write-Host "`n==> Repairing model" -ForegroundColor Cyan
    if ($Clean) { & $Py -m clipper whisper repair --clean } else { & $Py -m clipper whisper repair }
    if ($LASTEXITCODE -ne 0) { throw "Whisper model repair failed (see messages above)." }
}

Write-Host "`n==> 2. Generating test speech with the Windows voice" -ForegroundColor Cyan
$Text = "Grand Theft Auto six is finally here. Watch this car jump over the bridge. That was absolutely insane, chat."
$Wav = Join-Path $TestDir "speech.wav"
$Txt = Join-Path $TestDir "speech.txt"
Set-Content -Path $Txt -Value $Text -Encoding UTF8
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$english = $synth.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -like "en-*" } | Select-Object -First 1
if (-not $english) { throw "No English Windows voice installed (Settings > Time & language > Speech > Add voices)." }
$synth.SelectVoice($english.VoiceInfo.Name)
$synth.Rate = -1
$synth.SetOutputToWaveFile($Wav)
$synth.Speak($Text)
$synth.Dispose()

Write-Host "`n==> 3. Transcribing the speech (must match the sentence)" -ForegroundColor Cyan
& $Py -m clipper whisper verify --media $Wav --expect-file $Txt
if ($LASTEXITCODE -ne 0) { throw "Transcription check FAILED." }

Write-Host "`n==> 4. Full pipeline on the existing test video with speech mixed in (~2 min)" -ForegroundColor Cyan
$Video = Join-Path $TestDir "stream.mp4"
if (-not (Test-Path $Video)) { & $Py scripts\make_test_stream.py --out $Video }
$SpeechVideo = Join-Path $TestDir "stream_speech.mp4"
& $Py scripts\add_speech.py --video $Video --speech $Wav --at 41 --out $SpeechVideo
# Isolated, freshly wiped data folder: earlier test clips in your real database
# (same footage, same capture times) would correctly be rejected as duplicates.
# Duplicate detection stays fully enabled; the Whisper model (HF_HOME) is shared.
$VerifyDir = Join-Path $DataDir "verify-captions"
if (Test-Path $VerifyDir) { Remove-Item -Recurse -Force $VerifyDir }
& $Py -m clipper --data-dir $VerifyDir run --file $SpeechVideo --chat (Join-Path $TestDir "chat.jsonl") --channel CaptionTest
& $Py -m clipper --data-dir $VerifyDir whisper verify --latest-clip --expect-file $Txt
if ($LASTEXITCODE -ne 0) { throw "Pipeline caption check FAILED." }
Write-Host "`nWhisper verified: model complete, transcription correct, captions burned into the clip." -ForegroundColor Green
Write-Host "Watch it: $(Join-Path $VerifyDir 'outbox')"
