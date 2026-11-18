# GTA VI Live Clipper

Automated clipping for **authorized** GTA VI live streams on Twitch and YouTube:
capture → moment detection → clip extraction → dedupe/quality gate → 9:16 edit →
burned subtitles → metadata → publishing, with a small web dashboard.

Everything heavy runs locally (FFmpeg, faster-whisper). No paid service is required;
the only optional paid call (LLM metadata) is hard-capped by a spend ledger.

## Status (what is verified and what is not)

| Component | Status |
|---|---|
| Rolling buffer (FFmpeg segmenter), extraction, frame-accurate cuts | **Tested end-to-end** on a simulated live input |
| Moment detection (chat rate, hype terms, audio loudness, scene cuts) | **Tested**: unit tests and the end-to-end run |
| Quality gate (duration, black frames, silence) + dedupe (window overlap + frame dHash) | **Tested** |
| 9:16 vertical edit, credit line, burned captions, loudness normalisation | **Tested** (1080×1920 H.264/AAC output inspected) |
| Template metadata (title, description with creator credit, tags) | **Tested** |
| Dry-run publisher (outbox folder) + dashboard API | **Tested** |
| Dashboard authentication (every route except `/healthz`) | **Tested** with unit tests and over real HTTP against the Docker container |
| Docker image (Ubuntu 24.04, non-root, healthcheck) | **Built and run**: offline end-to-end test passed inside the container |
| Twitch Helix discovery / Create Clip, YouTube discovery, YouTube resumable upload | **Tested against mocked HTTP only**. Run `scripts/check_integrations.py` on your server with real keys |
| Twitch IRC chat, streamlink capture of real streams | **Not verified**: the build sandbox blocks twitch.tv. Verify with `scripts/check_integrations.py` |
| faster-whisper transcription | **Not verified**: the sandbox blocks the model download from huggingface.co. The code falls back to no captions when the model can't load. SRT generation and burning were tested with hand-made word timings |
| LLM metadata (optional) | Implemented with a budget gate. Not exercised: no API key here, and it is off by default |
| OpenShorts | **Not integrated.** Its interfaces haven't been verified. `data/outbox/` (video + JSON metadata) is the hand-off point if you want to feed clips into a self-hosted instance |

## Platform realities

- **Authorization**: only channels in `authorized_channels` with `authorized: true` are ever
  captured. Discovery lists other streams as "discovered (not authorized)" so you can ask
  the creator for permission. Every description carries a credit line and a source link.
- **YouTube uploads** (`videos.insert`) cost about 1,600 quota units out of the default
  10,000/day, so `max_uploads_per_day` defaults to 5. **Uploads from API projects that haven't
  passed Google's compliance audit are locked to private.** Apply for the audit before you
  expect public Shorts.
- **YouTube discovery**: `search.list` costs 100 units. The orchestrator therefore searches only
  your authorized YouTube channel IDs. Live chat polling also costs quota.
- **Twitch has no API for uploading edited videos.** "Twitch publishing" here means the official
  *Create Clip* endpoint (`publish.mode: twitch_clip`). It creates a native Twitch clip of the last
  ~30 s of the live broadcast when a moment is detected. It needs a user token with `clips:edit`,
  and the broadcaster must have clips enabled.
- TikTok, Instagram and similar platforms are not implemented.

## Quick start

```bash
pip install -r requirements.txt          # also needs ffmpeg on PATH
cp config/config.example.yaml config/config.yaml
cp .env.example .env                      # fill in only what you use
```

Offline end-to-end test (no network or keys needed):

```bash
python scripts/make_test_stream.py        # writes data/test/stream.mp4 + chat.jsonl
python -m clipper run --file data/test/stream.mp4 --chat data/test/chat.jsonl --channel TestStreamer
```

The finished vertical clip and its metadata JSON land in `data/outbox/`.

One authorized live stream (milestone 1):

```bash
python -m clipper run --twitch <channel> --channel-id <broadcaster_id>
python -m clipper run --youtube "https://www.youtube.com/watch?v=<id>" --channel-id UC... --live-chat-id <id>
```

Autonomous mode with the dashboard on http://127.0.0.1:8080:

```bash
python -m clipper serve
```

Add `--twitch <channel>` to also start one authorized channel immediately.

Every dashboard route needs HTTP Basic auth: any username, with `DASHBOARD_TOKEN` (at least 20
random characters) as the password. The server refuses to start without a strong token.

Pre-flight check (prints only set/unset for secrets):

```bash
python -m clipper doctor
```

Tests (about 2 minutes; includes a real-time end-to-end run):

```bash
python -m pytest -q
```

Live integration checks, run on the server with real keys. All checks are read-only; nothing is uploaded:

```bash
python scripts/check_integrations.py --twitch-channel <live_channel> --youtube-url <live_url>
```

## Credentials

All secrets come from environment variables or `.env`, which is git-ignored. They are never
written to the config, the database or the logs, and the dashboard reports them only as
set/unset.

| Variable | Used for | How to get it |
|---|---|---|
| `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` | Helix discovery | dev.twitch.tv console → register app |
| `TWITCH_USER_TOKEN` | Create Clip (optional) | OAuth user token with `clips:edit` |
| `YOUTUBE_API_KEY` | discovery, live chat | Google Cloud → YouTube Data API v3 → API key |
| `YOUTUBE_CLIENT_ID/SECRET` | uploads | Google Cloud → OAuth client (Desktop app) |
| `YOUTUBE_REFRESH_TOKEN` | uploads | `python scripts/youtube_auth.py` |
| `ANTHROPIC_API_KEY` | optional LLM metadata | console.anthropic.com |
| `DASHBOARD_TOKEN` | dashboard login (all routes) | `openssl rand -hex 24` |

Twitch chat is read anonymously (`justinfan` login), so it needs no token.

## How it works

```
discovery (Helix / Data API) → ranker → allowlist gate
   ↓ per authorized stream (max_streams)
streamlink → ffmpeg -f segment (4 s .ts, CSV index) → rolling buffer (buffer_s, pinned while extracting)
   ├─ per segment: one ffmpeg pass → audio RMS/peak + scene-change score   (~0.3 s CPU per segment)
   ├─ chat: Twitch IRC / YouTube liveChat → messages + hype terms per segment window
   └─ detector: robust z-scores (median/MAD baseline) → weighted score → threshold + cooldown
        ↓ moment (pre_roll 20 s, extended while the score keeps rising, post_roll 10 s)
extract (concat + re-encode, frame-accurate) → single worker queue:
   quality gate → dedupe → whisper (word timestamps → 4-word captions) → 9:16 edit + burn
   → metadata (templates; optional LLM under budget) → publish (retries, daily cap) → SQLite status
```

Code map: `clipper/capture.py`, `signals/chat.py`, `detect.py`, `quality.py`, `transcribe.py`,
`edit.py`, `metadata.py`, `publish/`, `discovery/`, `pipeline.py` (orchestration),
`dashboard/`, `budget.py`, `storage.py`.

Tuning lives in `config/config.yaml`: `detection.threshold` (lower it for more clips),
`detection.weights`, `hype_terms`, the pre/post roll, and the cooldown. The dashboard shows a
live score sparkline for each stream to help with this.

## Cost and resources (₹8,000/month budget)

**For the first real-world test (one stream), follow [docs/CHECKLIST.md](docs/CHECKLIST.md)** (short) or
[docs/DEPLOY_VPS.md](docs/DEPLOY_VPS.md) (detailed).
It has measured numbers, the exact VPS spec and the step-by-step setup.

Measured on a 4-vCPU container:

- Per-stream analysis: about 0.3 s of CPU per 4 s segment, roughly 8% of one core at 720p.
  Stream-copy capture is close to free.
- Vertical encode of a 34 s clip at `veryfast` with 2 threads: about 26 s.
- Whisper `small` int8 on CPU: not measured here, because the model download is blocked. Expect
  roughly real-time or better on 2 threads. Use `base` if the CPU can't keep up.

Recommendation: a single **4 vCPU / 8 GB VPS with 80+ GB disk** handles 3 simultaneous streams.
Such servers typically cost around ₹1,500–3,500/month at budget providers; check current prices
before you buy. That leaves most of the budget unused. Egress for 3 × 720p capture is about
1–1.5 TB/month, so pick a plan with enough bandwidth included.

Guards:

- `budget.ai_monthly_cap_inr: 500`. Each LLM call reserves its estimated cost *before* the request.
  When the cap is reached, metadata silently falls back to templates.
- Nothing in the code raises a cap at runtime.
- `storage.max_disk_gb`, retention days, the per-stream rolling buffer size and the buffer cleanup
  at stream end keep disk use bounded.
- One encode or transcribe job runs at a time, and `max_streams` limits concurrent captures.

If you enable LLM metadata, `metadata.llm_model` defaults to `claude-haiku-5-5`, which costs
far less than ₹1 per clip. Set it to any model you prefer.

## Deployment

```bash
cp config/config.vps-1stream.yaml config/config.yaml   # edit allowlist + settings
cp .env.example .env                               # add secrets; chmod 600 .env
docker compose up -d --build
ssh -L 8080:127.0.0.1:8080 user@server             # open http://127.0.0.1:8080
```

The dashboard binds to localhost by default and always requires authentication. Reach it through
an SSH tunnel. If you ever publish it, put it behind a TLS reverse proxy: Basic auth over plain
HTTP is not safe on the open internet. Persistent state lives in `./data`: the SQLite DB, clips,
outbox and the whisper model cache.
