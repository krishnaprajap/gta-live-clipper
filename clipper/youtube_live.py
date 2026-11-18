"""YouTube Live URL validation, live-status check and channel identification.

Live check, in order of preference:
  1. YouTube Data API v3 videos.list (needs YOUTUBE_API_KEY, 1 quota unit):
     authoritative live/upcoming/ended state and channel ID.
  2. streamlink's YouTube resolver (`streamlink --json`, no key needed): it
     resolves only *live* streams and explicitly refuses VOD content.

Capture never falls back to downloading a VOD: a URL that is not live right
now is rejected, and the watcher re-checks the live state before every
reconnect. Errors from YouTube/streamlink are reported verbatim.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from urllib.parse import parse_qs, urlparse

import httpx

from .system import streamlink_cmd

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
API = "https://www.googleapis.com/youtube/v3"


@dataclass
class ParsedURL:
    kind: str                 # "video" | "channel"
    video_id: str = ""
    handle: str = ""          # "@name"
    channel_id: str = ""      # "UC..."
    url: str = ""             # canonical URL to hand to streamlink


@dataclass
class LiveInfo:
    status: str               # live | not_live | upcoming | invalid | error
    message: str = ""
    video_id: str = ""
    channel_id: str = ""
    handle: str = ""
    channel_title: str = ""
    title: str = ""
    viewers: int = 0
    live_chat_id: str = ""
    method: str = ""          # api | streamlink
    watch_url: str = ""
    raw_error: str = ""

    @property
    def live(self) -> bool:
        return self.status == "live"

    def as_dict(self) -> dict:
        return asdict(self)


def parse_url(url: str) -> ParsedURL | None:
    """Accepts watch?v=, youtu.be/, /live/<id>, /@handle[/live], /channel/UC.../live.
    Returns None for anything that cannot be a live stream (shorts, playlists,
    other sites, malformed)."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    try:
        u = urlparse(url)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if host not in HOSTS:
        return None
    parts = [p for p in u.path.split("/") if p]
    if host.endswith("youtu.be"):
        vid = parts[0] if parts else ""
        return ParsedURL("video", video_id=vid, url=f"https://www.youtube.com/watch?v={vid}") if VIDEO_ID.match(vid) else None
    if parts[:1] == ["watch"]:
        vid = parse_qs(u.query).get("v", [""])[0]
        return ParsedURL("video", video_id=vid, url=f"https://www.youtube.com/watch?v={vid}") if VIDEO_ID.match(vid) else None
    if len(parts) == 2 and parts[0] == "live" and VIDEO_ID.match(parts[1]):
        return ParsedURL("video", video_id=parts[1], url=f"https://www.youtube.com/watch?v={parts[1]}")
    if parts and parts[0].startswith("@") and len(parts) <= 2 and (len(parts) == 1 or parts[1] == "live"):
        h = parts[0]
        return ParsedURL("channel", handle=h, url=f"https://www.youtube.com/{h}/live") if re.match(r"^@[\w.\-]{3,30}$", h) else None
    if len(parts) in (2, 3) and parts[0] == "channel" and CHANNEL_ID.match(parts[1]) and (len(parts) == 2 or parts[2] == "live"):
        return ParsedURL("channel", channel_id=parts[1], url=f"https://www.youtube.com/channel/{parts[1]}/live")
    return None


def _handle_from_author_url(author_url: str) -> tuple[str, str]:
    m = re.search(r"youtube\.com/(@[\w.\-]+)", author_url or "")
    c = re.search(r"youtube\.com/channel/(UC[\w-]{22})", author_url or "")
    return (m.group(1) if m else ""), (c.group(1) if c else "")


def streamlink_probe(url: str, timeout: float = 45) -> dict:
    """Ask streamlink to resolve the live stream (manifest only, no download)."""
    try:
        p = subprocess.run([*streamlink_cmd(), "--json", url, "best"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"streamlink timed out after {timeout:.0f}s resolving the stream"}
    try:
        return json.loads(p.stdout or "{}") or {"error": (p.stderr or "no output").strip()[-500:]}
    except json.JSONDecodeError:
        return {"error": ((p.stdout or "") + (p.stderr or "")).strip()[-500:] or "unreadable streamlink output"}


def _api_video(video_id: str, key: str, http: httpx.Client) -> LiveInfo:
    r = http.get(f"{API}/videos", params={"part": "snippet,liveStreamingDetails", "id": video_id, "key": key})
    if r.status_code != 200:
        return LiveInfo("error", f"YouTube API HTTP {r.status_code}", raw_error=r.text[:400], method="api")
    items = r.json().get("items", [])
    if not items:
        return LiveInfo("invalid", "video not found, private or deleted", video_id=video_id, method="api")
    v = items[0]
    sn, live = v.get("snippet", {}), v.get("liveStreamingDetails", {})
    state = sn.get("liveBroadcastContent", "none")
    info = LiveInfo(status={"live": "live", "upcoming": "upcoming"}.get(state, "not_live"), video_id=video_id,
                    channel_id=sn.get("channelId", ""), channel_title=sn.get("channelTitle", ""), title=sn.get("title", ""),
                    viewers=int(live.get("concurrentViewers", 0) or 0), live_chat_id=live.get("activeLiveChatId", ""),
                    method="api", watch_url=f"https://www.youtube.com/watch?v={video_id}")
    if info.status == "not_live":
        info.message = "stream has ended" if live.get("actualEndTime") else "this is not a live stream (VOD/upload)"
    elif info.status == "upcoming":
        info.message = f"stream is scheduled, not live yet (starts {live.get('scheduledStartTime', '?')})"
    if info.channel_id:
        c = http.get(f"{API}/channels", params={"part": "snippet", "id": info.channel_id, "key": key})
        if c.status_code == 200 and c.json().get("items"):
            cu = c.json()["items"][0]["snippet"].get("customUrl", "")
            info.handle = cu if cu.startswith("@") else (f"@{cu}" if cu else "")
    return info


def resolve(url: str, api_key: str | None = None, http: httpx.Client | None = None, probe=streamlink_probe) -> LiveInfo:
    p = parse_url(url)
    if not p:
        return LiveInfo("invalid", "not a YouTube live URL (use youtube.com/watch?v=…, youtu.be/…, "
                                   "youtube.com/live/…, youtube.com/@handle/live or youtube.com/channel/UC…/live)")
    http = http or httpx.Client(timeout=20)
    if api_key and p.kind == "video":
        try:
            return _api_video(p.video_id, api_key, http)
        except httpx.HTTPError as e:
            return LiveInfo("error", f"YouTube API unreachable: {type(e).__name__}: {e}", method="api")

    # streamlink path (also used for channel URLs to find the current live video)
    res = probe(p.url)
    if "error" in res:
        err = str(res["error"])
        low = err.lower()
        if "vod" in low:
            st, msg = "not_live", "this video is not live (VOD/ended); VOD download is not supported by design"
        elif "no playable streams" in low or "could not find" in low or "no live" in low or "offline" in low:
            st, msg = "not_live", "no live stream at this URL right now"
        elif "unable to validate" in low or "404" in low or "unavailable" in low or "private" in low:
            st, msg = "invalid", "video unavailable, private or not found"
        else:
            st, msg = "error", "YouTube/streamlink error"
        return LiveInfo(st, msg, video_id=p.video_id, handle=p.handle, channel_id=p.channel_id,
                        method="streamlink", raw_error=err)
    meta = res.get("metadata") or {}
    vid = meta.get("id") or p.video_id
    if api_key and vid and VIDEO_ID.match(vid):
        try:
            info = _api_video(vid, api_key, http)  # authoritative channel id + live chat id
            if info.status != "error":
                return info
        except httpx.HTTPError:
            pass
    info = LiveInfo("live", video_id=vid, channel_title=meta.get("author", ""), title=meta.get("title", ""),
                    handle=p.handle, channel_id=p.channel_id, method="streamlink",
                    watch_url=f"https://www.youtube.com/watch?v={vid}" if vid else p.url)
    if not (info.handle or info.channel_id) and vid:
        try:  # oEmbed gives the channel URL (@handle) without an API key
            o = http.get("https://www.youtube.com/oembed", params={"url": info.watch_url, "format": "json"})
            if o.status_code == 200:
                info.handle, cid = _handle_from_author_url(o.json().get("author_url", ""))
                info.channel_id = info.channel_id or cid
        except httpx.HTTPError:
            pass
    return info


def authorization(cfg, info: LiveInfo) -> tuple[bool, str]:
    """The channel must be on the allowlist (authorized: true) by channel ID or @handle."""
    keys = [k for k in (info.channel_id, info.handle) if k]
    if not keys:
        return False, "could not identify the channel (set YOUTUBE_API_KEY for reliable channel IDs)"
    for k in keys:
        if cfg.authorized("youtube", k):
            return True, f"authorized via allowlist entry {k}"
    return False, (f"channel {' / '.join(keys)} is not authorized. Add it to authorized_channels in config/config.yaml "
                   f"(platform: youtube, channel: {keys[0]}, authorized: true, permission_note: ...) only if you own it "
                   f"or have the creator's explicit permission")
