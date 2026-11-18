# Running on your Windows PC

Everything runs locally: capture, detection, Whisper captions, editing and the dashboard.
There is no server and no paid service. All clips and data stay in one folder you choose.

**Requirements:** Windows 10 (21H2+) or 11, 64-bit; 8 GB RAM or more recommended (4 GB works with
the `base` Whisper model); free disk space ≥ `storage.max_disk_gb` + `min_free_gb` (60 GB by default);
`winget` (built into Windows 11, or "App Installer" from the Microsoft Store on Windows 10).

## Install (once)

Download the repository from GitHub (Code → Download ZIP), or clone it, to e.g. `C:\GTAClipper\app`.
Open **PowerShell** (no admin needed) and run:

```powershell
cd C:\GTAClipper\app
```

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\setup.ps1 -DataDir D:\GTAClipper\data
```

`-DataDir` is where every clip, buffer, log, database and Whisper model is stored. Pick any local
drive; the default is `C:\GTAClipper\data`. Double-clicking `setup-windows.cmd` does the same with
the default folder.

The setup script:

1. Installs Python 3.12, FFmpeg (Gyan full build) and the VC++ runtime with `winget`, if missing.
2. Creates `.venv` with Streamlink, Faster-Whisper and the other dependencies.
3. Writes `config\config.yaml` and `.env`. The `.env` gets a random dashboard password and is
   readable only by your Windows account.
4. Downloads the Whisper model: `small`, or `base` if the PC has under 8 GB RAM.
5. Runs `doctor` and an offline self-test that produces one clip in `<DataDir>\outbox`.
6. Registers autostart: the clipper starts 1 minute after you log on to Windows.

## Configure your stream (once)

```powershell
notepad config\config.yaml
```

Set `channel:` to the creator's Twitch login, `authorized: true`, and the `permission_note`.

```powershell
notepad .env
```

Fill in `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET`. They're free: dev.twitch.tv → Console →
Register Your Application, with any OAuth redirect such as `http://localhost`, category "Other".
They enable the automatic start and reconnect whenever the channel goes live in GTA VI.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\restart.ps1
```

## Fix and verify Whisper captions

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\verify-whisper.ps1
```

This script:

1. **Diagnoses** the model cache in `<DataDir>\hf`. It reports interrupted downloads (`*.incomplete`),
   missing files, and files whose size or SHA-256 doesn't match the Hugging Face Hub.
2. **Repairs** problems by resuming interrupted downloads (with retries and a 60 s timeout for slow
   connections) and replacing corrupt files. If that isn't enough, it deletes the model and downloads
   it again. Add `-Clean` to force a clean download.
3. **Transcribes** a sentence spoken by the built-in Windows voice. The transcript must match it.
4. **Runs the full clipping pipeline** on the existing test video with that speech mixed in. The new
   clip must have burned-in captions.

Individual commands:

```powershell
.venv\Scripts\python.exe -m clipper whisper status --deep
```

```powershell
.venv\Scripts\python.exe -m clipper whisper repair
```

```powershell
.venv\Scripts\python.exe -m clipper whisper repair --clean
```

```powershell
.venv\Scripts\python.exe -m clipper whisper verify --latest-clip
```

**Captions are never skipped silently.** If Whisper fails on a clip, the vertical video is still made,
but the clip is marked `caption_failed`. The error shows in the dashboard and log, and the clip is
**held back from publishing**. `doctor` fails while the model is incomplete. A clip with no speech
is logged as "no speech detected".

## Twitch Client ID and Client Secret

They're free. Never paste them into chats, screenshots or tickets.

1. Turn on two-factor authentication for your Twitch account: twitch.tv → profile picture →
   **Settings → Security and Privacy → Two-Factor Authentication**. Twitch requires it before you can
   register applications.
2. Go to **https://dev.twitch.tv/console** and log in with that account.
3. Click **Applications → Register Your Application** and fill in:
   - **Name:** something unique, e.g. `gta-clipper-<yourname>`. It can't contain "Twitch".
   - **OAuth Redirect URLs:** `http://localhost`
   - **Category:** `Application Integration`
   - **Client Type:** `Confidential`
   - Complete the captcha and click **Create**.
4. Click **Manage** next to the new application. The **Client ID** is shown on that page.
5. Click **New Secret** and confirm. The **Client Secret** is shown **once**. Copy it straight into the
   next step. If you lose it, click New Secret again; the old one stops working.
6. Store both in `.env` with the helper. It hides the secret while you type, checks it with Twitch
   before saving, re-locks `.env` to your Windows account, runs a read-only check of the whole Twitch
   integration (including 15 s of chat), and restarts the clipper:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\set-twitch-credentials.ps1
```

The app reads `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET` only from `.env`. If Windows already has
environment variables with the same names, they take precedence; remove them. To re-check at any time:

```powershell
.venv\Scripts\python.exe -m clipper twitch-check --chat 15
```

The check validates the credentials and finds the GTA VI category. For every authorized channel it
confirms the login exists and shows whether it's live and in which category. It optionally checks
`TWITCH_USER_TOKEN` for `clips:edit` and reads chat. It never prints secret values.

## Daily use

| What | Command (PowerShell in the app folder) | Or double-click |
|---|---|---|
| Open dashboard | http://127.0.0.1:8080 (user: anything, password below) | `start-clipper.cmd` |
| Show dashboard password | `Select-String -Path .env -Pattern '^DASHBOARD_TOKEN='` | |
| Start / restart | `powershell -ExecutionPolicy Bypass -File scripts\windows\restart.ps1` | `start-clipper.cmd` |
| Stop | `powershell -ExecutionPolicy Bypass -File scripts\windows\stop.ps1` | `stop-clipper.cmd` |
| Turn autostart off | `powershell -ExecutionPolicy Bypass -File scripts\windows\autostart.ps1 -Disable` | |
| Turn autostart on | `powershell -ExecutionPolicy Bypass -File scripts\windows\autostart.ps1 -Enable` | |
| Check setup | `.venv\Scripts\python.exe -m clipper doctor` | |
| Live log | `Get-Content D:\GTAClipper\data\logs\clipper.log -Wait -Tail 30` | |
| Finished clips | `D:\GTAClipper\data\outbox\` (video + metadata JSON) | |

The dashboard listens on `127.0.0.1` only, so other devices on your network can't reach it. It
always asks for the password.

## Automatic start, restart and reconnect

- **Windows logon:** the Task Scheduler task "GTA Clipper" runs `scripts\windows\start.ps1`
  hidden, 1 minute after logon. It runs as your user, so no admin or stored password is needed.
  It starts after you log in, not before.
- **Crash:** `start.ps1` restarts the app with a 10 s → 5 min backoff. It records the reason in
  `logs\supervisor.log` and keeps the previous crash output in `logs\stderr.prev.log`.
- **Stream offline or interrupted:** the Twitch watchdog checks every 30 s and starts or reconnects
  capture with backoff. A capture that hangs is restarted after 5 minutes without data.
- **No leftovers:** on Windows the app runs inside a Job Object, so if it dies, its FFmpeg and
  Streamlink processes are killed with it.

## Resource limits (config\config.yaml)

| Setting | Default | Effect |
|---|---|---|
| `resources.process_priority` | `below_normal` | Games, browser and OBS get the CPU first. FFmpeg/Streamlink inherit it; clip encodes run at idle priority |
| `resources.max_cpu_percent` | `60` | Hard cap on total CPU for the app and all its children (Windows Job Object) |
| `resources.max_memory_gb` | `auto` (40% of RAM, 3–10 GB) | Hard memory cap for the app and children |
| `discovery.max_streams` | `auto` | Simultaneous streams from your hardware (table below), capped by `max_streams_cap: 4` |
| `transcription.cpu_threads`, `edit.threads` | `2`, `2` | Threads for Whisper and for each clip encode (one clip at a time) |
| `storage.max_disk_gb` | `50` | Hard cap for the data folder; oldest clips are deleted first |
| `storage.min_free_gb` | `10` | New clips are skipped (and logged) when the drive has less free space |
| `storage.keep_clips_days` / `keep_published_days` | `7` / `2` | Retention; cleanup runs at start and every 10 minutes |

`max_streams: auto` reserves 2 logical cores and 4 GB of RAM for Windows, the encoder and Whisper.
Each extra stream needs about 2 logical cores and 1 GB:

| PC | Streams |
|---|---|
| 4 threads / 8 GB | 1 |
| 6 threads / 8 GB | 2 |
| 8 threads / 16 GB | 3 |
| 12+ threads / 16+ GB | 4 (cap) |

`doctor` prints the number for your PC.

**Internet:** each stream downloads about 3 Mbps at 720p30, or up to 6 Mbps at 720p60, continuously.
That is roughly 1.4–2.7 GB per live hour per stream. Make sure your plan's data allowance covers it.

## Tested vs. not tested

- **Tested on Linux, which has the same Python code paths:** the full local flow the setup script
  performs. That covers a fresh venv, `pip install -r requirements.txt`, the Windows config profile
  with a separate data folder, `doctor`, the offline self-test producing a 1080×1920 clip in the
  outbox, the dashboard on localhost with password, and the rotating log file. 46 automated tests
  pass. Windows-only logic is also covered by tests: Job Object structure sizes match Win64, path
  escaping for `C:\` paths, fonts, UTF-8 BOM handling, and the stream-count formula.
- **Not executed on Windows:** the build environment has no Windows or PowerShell. `setup.ps1`,
  `start.ps1`, `stop.ps1`, `restart.ps1`, `autostart.ps1` and the Job Object calls are written to
  documented APIs but haven't been run. If a step fails, the script stops with a message; send it
  along.
- **Not tested at all:** live Twitch/YouTube (blocked in the build environment) and the Whisper model
  download (blocked there too; `setup.ps1` does it on your PC).
