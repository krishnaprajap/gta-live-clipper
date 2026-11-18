"""Chat activity signals.

ChatWindow keeps recent (wall_time, text) messages and answers "how many
messages / hype messages arrived in this wall-clock window".

TwitchChat: anonymous read-only IRC (justinfan login, no token needed).
YouTubeChat: polls liveChatMessages.list. Each poll costs YouTube Data API
quota, so it honours pollingIntervalMillis and a configurable minimum.
ReplayChat: feeds recorded/synthetic chat for offline tests.
"""
from __future__ import annotations

import asyncio
import bisect
import json
import random
import re
import ssl
import time
from collections import deque
from pathlib import Path

import httpx


class ChatWindow:
    def __init__(self, hype_terms: list[str], max_age_s: float = 900):
        self.times: deque[float] = deque()
        self.hype: deque[bool] = deque()
        self.max_age = max_age_s
        words = [t for t in hype_terms if re.match(r"^[\w ]+$", t)]
        self.symbols = [t for t in hype_terms if t not in words]
        self.rx = re.compile(r"\b(" + "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)) + r")\b",
                             re.I) if words else None
        self.total = 0

    def is_hype(self, text: str) -> bool:
        t = text.strip()
        if self.rx and self.rx.search(t):
            return True
        if any(s in t for s in self.symbols):
            return True
        letters = [c for c in t if c.isalpha()]
        return len(letters) >= 5 and sum(c.isupper() for c in letters) / len(letters) > 0.8

    def add(self, text: str, ts: float | None = None) -> None:
        ts = ts if ts is not None else time.time()
        self.times.append(ts)
        self.hype.append(self.is_hype(text))
        self.total += 1
        while self.times and self.times[0] < ts - self.max_age:
            self.times.popleft()
            self.hype.popleft()

    def stats(self, t0: float, t1: float) -> tuple[int, int]:
        times = list(self.times)
        i, j = bisect.bisect_left(times, t0), bisect.bisect_right(times, t1)
        hype = list(self.hype)[i:j]
        return j - i, sum(hype)


class TwitchChat:
    HOST, PORT = "irc.chat.twitch.tv", 6697

    def __init__(self, channel: str, window: ChatWindow):
        self.channel = channel.lower().lstrip("#")
        self.window = window
        self.connected = False
        self.error: str | None = None

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 2
        while not stop.is_set():
            try:
                await self._session(stop)
                backoff = 2
            except (OSError, asyncio.IncompleteReadError, ConnectionError) as e:
                self.connected = False
                self.error = f"{type(e).__name__}: {e}"
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _session(self, stop: asyncio.Event) -> None:
        reader, writer = await asyncio.open_connection(self.HOST, self.PORT, ssl=ssl.create_default_context())
        nick = f"justinfan{random.randint(10000, 99999)}"
        writer.write(f"NICK {nick}\r\nJOIN #{self.channel}\r\n".encode())
        await writer.drain()
        self.connected = True
        try:
            while not stop.is_set():
                line = await asyncio.wait_for(reader.readline(), timeout=360)
                if not line:
                    raise ConnectionError("IRC closed")
                msg = line.decode(errors="replace").rstrip("\r\n")
                if msg.startswith("PING"):
                    writer.write(("PONG" + msg[4:] + "\r\n").encode())
                    await writer.drain()
                    continue
                text = parse_privmsg(msg)
                if text is not None:
                    self.window.add(text)
        finally:
            self.connected = False
            writer.close()


def parse_privmsg(line: str) -> str | None:
    # ":nick!nick@nick.tmi.twitch.tv PRIVMSG #chan :message"  (tags optional)
    if line.startswith("@"):
        _, _, line = line.partition(" ")
    parts = line.split(" ", 3)
    if len(parts) == 4 and parts[1] == "PRIVMSG":
        return parts[3][1:] if parts[3].startswith(":") else parts[3]
    return None


class YouTubeChat:
    API = "https://www.googleapis.com/youtube/v3/liveChat/messages"

    def __init__(self, live_chat_id: str, api_key: str, window: ChatWindow, min_interval_s: float = 15,
                 on_quota=None, client: httpx.AsyncClient | None = None):
        self.live_chat_id = live_chat_id
        self.api_key = api_key
        self.window = window
        self.min_interval = min_interval_s
        self.on_quota = on_quota  # callback(units) for quota accounting
        self.client = client
        self.connected = False
        self.error: str | None = None

    async def run(self, stop: asyncio.Event) -> None:
        page_token = None
        client = self.client or httpx.AsyncClient(timeout=20)
        try:
            while not stop.is_set():
                params = {"liveChatId": self.live_chat_id, "part": "snippet", "key": self.api_key, "maxResults": 2000}
                if page_token:
                    params["pageToken"] = page_token
                try:
                    r = await client.get(self.API, params=params)
                    if self.on_quota:
                        self.on_quota(5)
                    if r.status_code != 200:
                        self.error = f"HTTP {r.status_code}: {r.text[:200]}"
                        self.connected = False
                        if r.status_code in (403, 404):  # quota exceeded / chat ended
                            return
                        await asyncio.sleep(60)
                        continue
                    data = r.json()
                    self.connected = True
                    now = time.time()
                    for item in data.get("items", []):
                        text = item.get("snippet", {}).get("displayMessage", "")
                        self.window.add(text, now)
                    page_token = data.get("nextPageToken")
                    wait = max(self.min_interval, data.get("pollingIntervalMillis", 0) / 1000)
                except httpx.HTTPError as e:
                    self.error = str(e)
                    wait = 30
                await asyncio.sleep(wait)
        finally:
            if not self.client:
                await client.aclose()


class ReplayChat:
    """Replays a JSONL file of {"t": seconds_from_start, "text": "..."} lines
    in real time, relative to when run() starts."""

    def __init__(self, path: str | Path, window: ChatWindow):
        self.items = [json.loads(l) for l in Path(path).read_text(encoding="utf-8-sig").splitlines() if l.strip()]
        self.window = window
        self.connected = True
        self.error = None

    async def run(self, stop: asyncio.Event) -> None:
        t0 = time.time()
        for it in sorted(self.items, key=lambda x: x["t"]):
            delay = t0 + it["t"] - time.time()
            if delay > 0:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                    return
                except asyncio.TimeoutError:
                    pass
            self.window.add(it["text"])
