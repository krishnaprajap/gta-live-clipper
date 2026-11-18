# First real-world test: one stream on one VPS

This guide sets up a **single authorized Twitch stream** on a small VPS in dry-run mode.
Nothing is published: finished clips land in `data/outbox/` for you to review.

> **Not verified yet:** live Twitch/YouTube capture, chat, discovery and uploads have only been
> tested against mocked APIs. The build sandbox couldn't reach twitch.tv, and it couldn't download
> Whisper models from huggingface.co. This test is the first real verification. Record the results
> (see step 13).

## 1. Recommended server

| | Minimum (recommended for this test) | Step-up if the minimum struggles |
|---|---|---|
| Plan | Hetzner Cloud **CX23** (shared vCPU, cost-optimized) | Hetzner Cloud **CX33** |
| vCPU | 2 (x86, AVX2) | 4 |
| RAM | 4 GB (+ 2 GB swap file) | 8 GB |
| Disk | 40 GB NVMe | 80 GB NVMe |
| Traffic | 20 TB/month included (EU locations) | 20 TB/month included |
| Location | Falkenstein / Nuremberg / Helsinki | same |
| OS | Ubuntu 24.04 LTS | same |
| Public IPv4 | yes (needed for SSH from most Indian ISPs) | yes |
| Price (reported) | €5.49 + €0.50 IPv4 = **€5.99/month** excl. tax | €8.49 + €0.50 = **€8.99/month** |
| ≈ INR (at ₹104/€) | **₹625**, or **≈ ₹780** worst case with 18% GST + card forex fees | **₹935**, or **≈ ₹1,170** worst case |

Notes:

- I couldn't open hetzner.com from the build sandbox. The prices above come from third-party
  trackers describing the 15 June 2026 price change, and those trackers report CX stock sometimes
  being unavailable. **Check the price and availability on the order page before you buy.**
- Use x86 plans, not the ARM CAX line. The image and benchmarks were built for x86-64.
- Optional Hetzner backups add 20% (about ₹125/month on CX23). This test doesn't need them.
- Any provider with the same specs works. Check that it offers ≥ 1 TB/month of traffic, x86 with
  AVX2, and Ubuntu 24.04.

## 2. Expected resource use for ONE stream

**Measured** in the sandbox with a deliberately hard input (720p60, 6 Mbps, full-frame noise):

| Item | Measured |
|---|---|
| Segment analysis (audio + scene) | 0.50 CPU-s per 4 s segment, about **12.5% of one core** |
| Clip: extract + quality + fingerprint + 9:16 encode (34 s clip) | about **130 CPU-s**. The encode alone is 93 CPU-s, or 48 s wall time on 2 threads |
| Peak RAM of the whole pipeline while encoding (no Whisper) | **501 MB**. The largest single ffmpeg was 390 MB |
| Files per clip | raw 38.6 MB + vertical 21.1 MB + outbox copy 21.1 MB ≈ **81 MB** |
| Docker image | **1.64 GB** |

**Estimated** (not measured here: needs the real model or a real stream):

| Item | Estimate |
|---|---|
| Whisper `small` int8, 2 threads, 34 s clip | 20–60 CPU-s and 0.5–1 GB RAM. **Measure it with `scripts/benchmark_whisper.py`** |
| streamlink + ffmpeg stream-copy capture | < 5% of one core |
| Ingest bitrate | 720p30 transcode ≈ 3 Mbps; 720p60/source up to ≈ 6 Mbps |

Totals for one stream, assuming 6 clips per live hour:

- **CPU:** about 0.5 core on average. Peaks reach 2 cores during an encode. Clip work runs at nice 10,
  so capture always gets priority. A clip is ready about 1–2.5 minutes after the moment ends.
- **RAM:** about 1.5–2.2 GB total (pipeline 0.5 + Whisper 0.5–1 + OS/Docker 0.5). 4 GB is enough;
  the container is capped at 3 GB.
- **Storage:** OS about 3 GB, Docker image plus build cache about 3 GB, Whisper model about 0.5 GB,
  rolling buffer 70–135 MB, logs ≤ 60 MB. Clips take about 81 MB each. At 36 clips/day with 3-day
  retention that is up to about 8.7 GB, and `storage.max_disk_gb: 15` caps it. **Peak ≈ 22 GB of 40 GB.**
- **Bandwidth (ingest):** 1.35 GB per live hour at 720p30, or 2.7 GB at 720p60. A streamer live
  6 h/day uses **≈ 245–490 GB/month**. Capturing 24/7 at 720p60 would be ≈ 1.95 TB. Egress is tiny:
  dry run uses none, and 5 YouTube uploads a day is about 3 GB/month. 20 TB included covers all of this.

## 3. Create the server (do this yourself)

1. Create a Hetzner Cloud project and add your **SSH public key**. Don't use password login.
2. Create the server with the plan, location and Ubuntu 24.04 settings from step 1.
3. Add a **Cloud Firewall** that allows inbound TCP 22 only, ideally from your own IP. The dashboard
   is never opened to the internet.

## 4. Harden the server

```bash
ssh root@SERVER_IP
```

```bash
adduser --disabled-password --gecos "" clip && usermod -aG sudo clip
```

```bash
rsync --archive --chown=clip:clip ~/.ssh /home/clip
```

```bash
echo 'clip ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/clip && chmod 440 /etc/sudoers.d/clip
```

```bash
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/; s/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config && systemctl restart ssh
```

```bash
ufw allow OpenSSH && ufw --force enable
```

```bash
apt-get update && apt-get -y upgrade && apt-get install -y unattended-upgrades vnstat git
```

Add a 2 GB swap file as a safety net for RAM spikes:

```bash
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

Log out and reconnect as `clip`:

```bash
ssh clip@SERVER_IP
```

## 5. Install Docker (official repository)

```bash
sudo install -m 0755 -d /etc/apt/keyrings && sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
```

```bash
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list
```

```bash
sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

```bash
sudo usermod -aG docker clip && newgrp docker
```

Docker-published ports bypass ufw. This compose file publishes only on `127.0.0.1`, so nothing is
exposed. Keep it that way.

## 6. Get the code (read-only access)

Create a read-only **deploy key** for the private repo:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/clipper_deploy -N ""
```

```bash
cat ~/.ssh/clipper_deploy.pub
```

Add the printed public key under GitHub → repo → Settings → Deploy keys, with write access **off**.

```bash
GIT_SSH_COMMAND="ssh -i ~/.ssh/clipper_deploy" git clone -b claude/gta-vi-clipping-system-aldkcr git@github.com:krishnaprajap/gta-live-clipper.git ~/clipper
```

```bash
cd ~/clipper
```

## 7. Configure

```bash
cp config/config.vps-1stream.yaml config/config.yaml
```

```bash
nano config/config.yaml
```

Set the creator's Twitch login, `authorized: true` and the permission note.

```bash
cp .env.example .env && chmod 600 .env
```

```bash
sed -i "s/^DASHBOARD_TOKEN=.*/DASHBOARD_TOKEN=$(openssl rand -hex 24)/" .env
```

The first test needs **only `DASHBOARD_TOKEN`**:

- Twitch chat is read anonymously, and streamlink needs no key for public streams.
- Leave every other variable empty until you test that feature.
- Never paste `.env` into chats, tickets or screenshots.
- To read the token once for the browser login: `grep DASHBOARD_TOKEN .env`.

| Variable | Needed for | First test? |
|---|---|---|
| `DASHBOARD_TOKEN` | dashboard login (≥ 20 chars; `change-me` is rejected) | **required** |
| `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` | auto start/reconnect watchdog + discovery (free) | **required** for auto-reconnect |
| `TWITCH_USER_TOKEN` | `publish.mode: twitch_clip` | no |
| `YOUTUBE_API_KEY` | YouTube discovery and chat | no |
| `YOUTUBE_CLIENT_ID/SECRET/REFRESH_TOKEN` | `publish.mode: youtube` | no |
| `ANTHROPIC_API_KEY` | `metadata.use_llm: true` | no |

Create the data directory, owned by the container's unprivileged user (uid 10001):

```bash
mkdir -p data && sudo chown -R 10001:10001 data
```

## 8. Build and run the pre-flight check

```bash
docker compose build
```

```bash
docker compose run --rm clipper doctor
```

`doctor` prints only whether each secret is set, never its value. It must end with
`all required checks passed`.

## 9. Offline end-to-end test on the VPS (no Twitch needed)

```bash
docker compose run --rm --entrypoint python3 clipper scripts/make_test_stream.py
```

```bash
docker compose run --rm clipper run --file data/test/stream.mp4 --chat data/test/chat.jsonl --channel VpsTest
```

Expected: one clip with `"status": "dry_run"` and a file in `data/outbox/`. This also downloads the
Whisper model into `data/hf/`. It is the first real check that the download works.

## 10. Benchmark Whisper on this VPS

`small` is the model the config uses. `--no-vad` makes it transcribe all audio, which is the worst case.

```bash
docker compose run --rm --entrypoint python3 clipper scripts/benchmark_whisper.py data/test/stream.mp4 --model small --no-vad
```

Read `RTF` (processing time ÷ audio length) and `peak_rss`:

- **RTF ≤ 1.5:** keep `small`.
- **RTF > 1.5 or peak_rss > 1500 MB:** set `transcription.model: base`.

For real accuracy, benchmark again later on a clip that contains speech.

## 11. Read-only live integration check

Run this **while the authorized streamer is live**. It reads chat for 20 s and captures 15 s of video
to a temp file, which it then deletes.

```bash
docker compose run --rm --entrypoint python3 clipper scripts/check_integrations.py --twitch-channel CHANNEL_LOGIN
```

## 12. First live test

Start this while the authorized streamer is live:

```bash
CLIPPER_COMMAND="serve --twitch CHANNEL_LOGIN" docker compose up -d
```

Open the dashboard through an SSH tunnel from your own computer:

```bash
ssh -N -L 8080:127.0.0.1:8080 clip@SERVER_IP
```

Then browse to http://127.0.0.1:8080. Use any username, and the `DASHBOARD_TOKEN` value as the password.

Monitor:

```bash
docker compose logs -f --tail 50
```

```bash
docker stats --no-stream
```

```bash
df -h / && du -sh data/*
```

```bash
vnstat -h
```

Automatic start and reconnect (needs `TWITCH_CLIENT_ID/SECRET`, which are free app credentials):

- Every `twitch_watch.poll_s` (30 s), one Helix request checks the authorized channels.
- When a channel is live in a `discovery.game_names` category, capture starts.
- If capture drops while the channel is still live, it reconnects. Each quick failure doubles the
  wait, from 10 s up to 600 s.
- A capture that hangs without exiting is restarted after `capture.stall_timeout_s` (300 s) with no
  data.
- Twitch chat reconnects by itself with backoff.
- A **Stop** from the dashboard stays stopped until the channel goes offline.
- With the watchdog running, plain `docker compose up -d` is enough; `--twitch` isn't needed.
- Without the credentials there is no auto-reconnect: run `up -d` again whenever the streamer is live.

Stop:

```bash
docker compose down
```

## 13. What to record (the real test report)

- `doctor` output, and the step 9 result.
- Step 10: RTF and peak memory for `small`, and for `base` if you tried it.
- Step 11 PASS/FAIL lines.
- During the live test:
  - number of moments and clips
  - how many clips were actually good (precision)
  - obvious moments that were missed
  - minutes from moment to clip
  - `docker stats` CPU% and memory
  - disk use after a few hours
  - `vnstat` GB per hour
- Problems seen: ad breaks, quality switches, chat disconnects, OOM kills (`dmesg | grep -i kill`).

Tune `detection.threshold` in `config/config.yaml`: raise it for fewer clips, lower it for more.
Then restart with `docker compose up -d`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `refusing to start dashboard: DASHBOARD_TOKEN is too weak` | Regenerate it as shown in step 7 |
| `attempt to write a readonly database` / permission denied in `data/` | `sudo chown -R 10001:10001 data` |
| Stream status `error`, log mentions "No playable streams" | The channel is offline, or the quality list doesn't match; `best` is the last fallback |
| Clips have no captions; event "could not load whisper model" | The VPS can't reach huggingface.co. Check outbound HTTPS, or set `transcription.enabled: false` |
| Container restarts; `dmesg` shows an OOM kill | Use `transcription.model: base`, or move up to CX33 |
| Clips fall far behind the live stream | Raise `detection.threshold` or use the `base` model; check `docker stats` |
