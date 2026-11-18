"""Live, read-only integration checks. Run on the deployment host after
filling .env. Each check is independent and reports PASS/FAIL/SKIP.

    python scripts/check_integrations.py [--twitch-channel name]
"""
import argparse
import asyncio
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from clipper.config import load_config, secret  # noqa: E402
from clipper.signals.chat import ChatWindow, TwitchChat  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--twitch-channel", help="a live channel to test chat + streamlink against")
ap.add_argument("--youtube-url", help="a live YouTube watch URL to test streamlink against")
a = ap.parse_args()
cfg = load_config()


def report(name, fn):
    try:
        msg = fn()
        print(f"[{'SKIP' if msg and msg.startswith('SKIP') else 'PASS'}] {name}: {msg or ''}")
    except Exception as e:
        print(f"[FAIL] {name}: {type(e).__name__}: {e}")


def twitch_api():
    if not (secret("TWITCH_CLIENT_ID") and secret("TWITCH_CLIENT_SECRET")):
        return "SKIP no TWITCH_CLIENT_ID/SECRET"
    from clipper.discovery.twitch import TwitchAPI
    s = TwitchAPI(secret("TWITCH_CLIENT_ID"), secret("TWITCH_CLIENT_SECRET")).live_streams(cfg["discovery"]["game_names"])
    return f"{len(s)} live GTA VI streams; top: {[(x.channel, x.viewers) for x in s[:3]]}"


def youtube_api():
    if not secret("YOUTUBE_API_KEY"):
        return "SKIP no YOUTUBE_API_KEY"
    from clipper.discovery.youtube import YouTubeAPI
    s = YouTubeAPI(secret("YOUTUBE_API_KEY")).live_streams("GTA 6", max_results=5)  # costs ~101 quota units
    return f"{len(s)} live results; top: {[(x.channel, x.viewers) for x in s[:3]]}"


def youtube_oauth():
    keys = [secret(k) for k in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN")]
    if not all(keys):
        return "SKIP no YouTube OAuth credentials"
    from clipper.publish.youtube import YouTubePublisher
    YouTubePublisher(*keys)._access_token()
    return "refresh token exchanged for an access token (no upload performed)"


def twitch_chat():
    if not a.twitch_channel:
        return "SKIP pass --twitch-channel"
    w = ChatWindow([])
    c = TwitchChat(a.twitch_channel, w)

    async def go():
        stop = asyncio.Event()
        t = asyncio.create_task(c.run(stop))
        await asyncio.sleep(20)
        stop.set()
        t.cancel()
    asyncio.run(go())
    if w.total == 0 and c.error:
        raise RuntimeError(c.error)
    return f"received {w.total} messages in 20 s"


def streamlink_capture(url):
    def fn():
        if not url:
            return "SKIP no URL"
        try:
            import streamlink  # noqa: F401
        except ImportError:
            raise RuntimeError("streamlink not installed")
        out = Path("data/check_capture.ts")
        out.parent.mkdir(exist_ok=True)
        p = subprocess.Popen([sys.executable, "-m", "streamlink", "-o", str(out), "-f", url, "480p,worst"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        time.sleep(15)
        p.terminate()
        log = p.communicate(timeout=10)[0].decode(errors="replace")
        size = out.stat().st_size if out.exists() else 0
        out.unlink(missing_ok=True)
        if size == 0:
            raise RuntimeError(log[-400:])
        return f"captured {size/1e6:.1f} MB in 15 s"
    return fn


report("Twitch Helix discovery", twitch_api)
report("Twitch IRC chat (anonymous)", twitch_chat)
report("streamlink Twitch capture", streamlink_capture(f"https://www.twitch.tv/{a.twitch_channel}" if a.twitch_channel else None))
report("YouTube Data API discovery", youtube_api)
report("YouTube OAuth (upload credentials)", youtube_oauth)
report("streamlink YouTube capture", streamlink_capture(a.youtube_url))
