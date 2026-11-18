"""YouTube Live ingestion tests.

Fast tests: URL validation, live-state detection (YouTube API and streamlink
paths, mocked), authorization, dashboard endpoints, publishing gate.

Live scenario (real, ~2.5 min): a local *live* HLS stream (FFmpeg encoding in
real time, playlist served over HTTP) is captured through the same YouTube
session code path: streamlink -> FFmpeg rolling buffer -> detection ->
extraction -> captions -> vertical render. Only the YouTube live check is a
stand-in (youtube.com cannot be reached from CI). The scenario: stream A goes
live, is interrupted (encoder killed), stream B comes back, a moment is
clipped while B is still live, then B ends and the session closes."""
import asyncio
import functools
import http.server
import json
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from clipper import youtube_live as yl
from clipper.config import load_config
from clipper.pipeline import Orchestrator
from clipper.transcribe import Word
from clipper.youtube_live import LiveInfo, parse_url, resolve

ROOT = Path(__file__).resolve().parent.parent
CID = "UC" + "a" * 22
VID = "AbCdEfGhIjK"


# ------------------------------------------------------------------ URL validation
@pytest.mark.parametrize("url,kind,vid", [
    ("https://www.youtube.com/watch?v=AbCdEfGhIjK", "video", VID),
    ("https://youtube.com/watch?feature=share&v=AbCdEfGhIjK&t=30", "video", VID),
    ("youtu.be/AbCdEfGhIjK", "video", VID),
    ("https://m.youtube.com/live/AbCdEfGhIjK?si=x", "video", VID),
    ("https://www.youtube.com/@MyChannel/live", "channel", ""),
    ("https://www.youtube.com/@MyChannel", "channel", ""),
    (f"https://www.youtube.com/channel/{CID}/live", "channel", ""),
])
def test_valid_live_urls(url, kind, vid):
    p = parse_url(url)
    assert p and p.kind == kind and p.video_id == vid
    assert p.url.startswith("https://www.youtube.com/")


@pytest.mark.parametrize("url", [
    "", "not a url", "https://vimeo.com/123", "https://www.youtube.com/shorts/AbCdEfGhIjK",
    "https://www.youtube.com/playlist?list=PL123", "https://www.youtube.com/watch?v=short",
    "https://youtube.com.evil.com/watch?v=AbCdEfGhIjK", "https://www.youtube.com/@x/videos", "javascript:alert(1)",
])
def test_invalid_urls(url):
    assert parse_url(url) is None
    assert resolve(url, probe=lambda u: pytest.fail("must not probe invalid URLs")).status == "invalid"


# ------------------------------------------------------------------ live state
def api_mock(state="live", items=True, end=False):
    def h(req):
        if req.url.path.endswith("/videos"):
            if not items:
                return httpx.Response(200, json={"items": []})
            live = {"concurrentViewers": "42", "activeLiveChatId": "chat1"}
            if end:
                live["actualEndTime"] = "2026-01-01T00:00:00Z"
            return httpx.Response(200, json={"items": [{"id": VID, "snippet": {
                "liveBroadcastContent": state, "channelId": CID, "channelTitle": "My Channel", "title": "GTA 6 live"},
                "liveStreamingDetails": live}]})
        if req.url.path.endswith("/channels"):
            return httpx.Response(200, json={"items": [{"snippet": {"customUrl": "@mychannel"}}]})
        if req.url.path == "/oembed":
            return httpx.Response(200, json={"author_url": "https://www.youtube.com/@mychannel"})
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(h))


def test_live_via_api():
    i = resolve(f"https://youtu.be/{VID}", api_key="k", http=api_mock("live"))
    assert i.live and i.channel_id == CID and i.handle == "@mychannel" and i.live_chat_id == "chat1" and i.viewers == 42


def test_live_via_streamlink_without_api_key():
    probe = lambda u: {"plugin": "youtube", "metadata": {"id": VID, "author": "My Channel", "title": "GTA 6 live"},
                       "streams": {"best": {}}}
    i = resolve(f"https://www.youtube.com/watch?v={VID}", http=api_mock(), probe=probe)
    assert i.live and i.method == "streamlink" and i.handle == "@mychannel" and i.watch_url.endswith(VID)


@pytest.mark.parametrize("state,end,expect", [("none", True, "ended"), ("none", False, "not a live stream"),
                                              ("upcoming", False, "scheduled")])
def test_not_live_via_api(state, end, expect):
    i = resolve(f"https://youtu.be/{VID}", api_key="k", http=api_mock(state, end=end))
    assert not i.live and expect in i.message


@pytest.mark.parametrize("err,status", [
    ("This plugin does not support VOD content, try yt-dlp instead", "not_live"),
    ("No playable streams found on this URL", "not_live"),
    ("Unable to open URL: ... 403 Forbidden", "error"),
])
def test_not_live_or_error_via_streamlink(err, status):
    i = resolve(f"https://youtu.be/{VID}", probe=lambda u: {"error": err})
    assert i.status == status and i.raw_error == err   # the real error is kept verbatim


def test_missing_video_is_invalid():
    assert resolve(f"https://youtu.be/{VID}", api_key="k", http=api_mock(items=False)).status == "invalid"


def test_authorization_by_channel_id_or_handle():
    cfg = load_config(overrides={"authorized_channels": [{"platform": "youtube", "channel": "@MyChannel", "authorized": True}]})
    assert yl.authorization(cfg, LiveInfo("live", handle="@mychannel"))[0]
    ok, msg = yl.authorization(cfg, LiveInfo("live", channel_id=CID, handle="@other"))
    assert not ok and "not authorized" in msg


# ------------------------------------------------------------------ dashboard endpoints
@pytest.fixture
def dash(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "t" * 32)
    cfg = load_config(overrides={"data_dir": str(tmp_path), "transcription": {"enabled": False},
                                 "authorized_channels": [{"platform": "youtube", "channel": CID, "authorized": True}]})
    from clipper.dashboard.app import create_app
    orch = Orchestrator(cfg)
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    launched = []
    monkeypatch.setattr(orch, "launch_youtube", lambda info, url: launched.append(info) or "youtube:x")
    c = TestClient(create_app(cfg, orch, loop), headers={"X-Token": "t" * 32})
    yield c, monkeypatch, launched
    loop.call_soon_threadsafe(loop.stop)


@pytest.mark.parametrize("info,code", [
    (LiveInfo("invalid", "not a YouTube live URL"), 400),
    (LiveInfo("not_live", "stream has ended"), 409),
    (LiveInfo("upcoming", "scheduled"), 409),
    (LiveInfo("error", "YouTube/streamlink error", raw_error="HTTP 403"), 502),
    (LiveInfo("live", channel_id="UC" + "b" * 22), 403),     # live but not on the allowlist
])
def test_dashboard_refuses(dash, info, code):
    c, mp, launched = dash
    mp.setattr(yl, "resolve", lambda url, key=None: info)
    r = c.post("/api/youtube/start", json={"url": "https://youtu.be/x", "permission_confirmed": True})
    assert r.status_code == code and not launched


def test_dashboard_requires_permission_confirmation(dash):
    c, mp, launched = dash
    mp.setattr(yl, "resolve", lambda url, key=None: LiveInfo("live", channel_id=CID))
    assert c.post("/api/youtube/start", json={"url": "https://youtu.be/x"}).status_code == 400
    r = c.post("/api/youtube/start", json={"url": "https://youtu.be/x", "permission_confirmed": True})
    assert r.status_code == 200 and launched


def test_dashboard_has_youtube_controls(dash):
    html = dash[0].get("/").text
    assert 'id="yturl"' in html and "START" in html and "stopStream" in html


# ------------------------------------------------------------------ publishing gate
def test_publishing_requires_publish_allowed(tmp_path):
    cfg = load_config(overrides={"data_dir": str(tmp_path), "transcription": {"enabled": False},
                                 "authorized_channels": [{"platform": "youtube", "channel": "@mine", "authorized": True}]})
    orch = Orchestrator(cfg)

    class Uploader:
        name = "youtube"
        def publish(self, video, meta):
            raise AssertionError("must not upload without publish_allowed")
    orch.processor.publisher = Uploader()
    m = orch.db.add_moment("youtube:@mine", 1, 5, {"channel_ids": [CID, "@mine"]}, "extracted")
    cid = orch.db.execute("INSERT INTO clips(moment_id,stream_key,status,created_at) VALUES(?,?,?,?)", (m, "youtube:@mine", "ready", time.time()))
    assert orch.processor.publish(cid, Path("x.mp4"), {"title": "t", "description": "d"})["status"] == "held"
    cfg.raw["authorized_channels"][0]["publish_allowed"] = True
    assert cfg.publish_allowed("youtube", [CID, "@mine"])


# ------------------------------------------------------------------ real live HLS scenario
class HLSProducer:
    """Real-time FFmpeg HLS encoder = a live stream."""
    def __init__(self, src: Path, out: Path, prefix: str, live_forever: bool):
        flags = "delete_segments+omit_endlist" if live_forever else "delete_segments"
        self.p = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-i", str(src), "-c", "copy",
                                   "-f", "hls", "-hls_time", "2", "-hls_list_size", "8", "-hls_flags", flags,
                                   "-hls_segment_filename", str(out / f"{prefix}_%04d.ts"), str(out / "live.m3u8")])
        self.ended_at = None

    def alive(self):
        if self.p.poll() is not None and self.ended_at is None:
            self.ended_at = time.time()
        return self.p.poll() is None

    def kill(self):
        self.p.send_signal(signal.SIGKILL)
        self.p.wait()
        self.ended_at = time.time()


def serve_dir(d: Path):
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(d)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class StandInTranscriber:
    def words(self, media):
        return [Word(1.0 + i * 0.4, 1.35 + i * 0.4, w) for i, w in enumerate("no way he jumped the bridge".split())], "en"


@pytest.fixture(scope="module")
def live_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ytlive")
    gen = [sys.executable, str(ROOT / "scripts/make_test_stream.py")]
    subprocess.run(gen + ["--out", str(tmp / "a.mp4"), "--duration", "40", "--event", "200"], check=True)  # calm
    subprocess.run(gen + ["--out", str(tmp / "b.mp4"), "--duration", "100", "--event", "34"], check=True)  # has a moment
    hls = tmp / "hls"
    hls.mkdir()
    srv = serve_dir(hls)
    url = f"hls://http://127.0.0.1:{srv.server_address[1]}/live.m3u8"

    cfg = load_config(overrides={
        "data_dir": str(tmp / "data"), "publish": {"mode": "dry_run"},
        "authorized_channels": [{"platform": "youtube", "channel": "@testchannel", "authorized": True}],
        "capture": {"segment_s": 4, "buffer_s": 20, "stall_timeout_s": 10, "stream_timeout_s": 8},
        "edit": {"width": 540, "height": 960},
        "youtube_watch": {"min_backoff_s": 1, "healthy_after_s": 5}})
    orch = Orchestrator(cfg)
    orch.processor.transcriber = StandInTranscriber()
    obs = {"states": [], "buffer_samples": [], "events": []}
    current = {"p": None}

    def resolver(u):
        p = current["p"]
        if p and p.alive():
            return LiveInfo("live", video_id=VID, handle="@testchannel", title="test live", watch_url=url, method="test")
        return LiveInfo("not_live", "stream has ended", method="test")

    def wait_playlist(n=3, timeout=30):
        t0 = time.time()
        while time.time() - t0 < timeout:
            m = hls / "live.m3u8"
            if m.exists() and m.read_text().count(".ts") >= n:
                return
            time.sleep(0.3)
        raise TimeoutError("producer did not start")

    async def scenario():
        key = None
        # --- stream A goes live
        current["p"] = HLSProducer(tmp / "a.mp4", hls, "a", live_forever=True)
        await asyncio.get_running_loop().run_in_executor(None, wait_playlist)
        info, ok, code, msg = orch.validate_youtube(f"https://youtu.be/{VID}", resolver=resolver)
        assert ok, msg
        key = orch.launch_youtube(info, f"https://youtu.be/{VID}")
        t0 = time.time()
        while time.time() - t0 < 60:
            w = orch.workers[key][0]
            obs["states"].append(w.state)
            if len(w.buffer.segments) >= 3:
                break
            await asyncio.sleep(0.5)
        obs["a_segments"] = len(orch.workers[key][0].buffer.segments)
        obs["a_session"] = orch.workers[key][0].buffer.out_dir.name
        # --- interruption: encoder dies without ENDLIST
        current["p"].kill()
        t0 = time.time()
        while not orch.workers[key][1].done() and time.time() - t0 < 60:
            await asyncio.sleep(0.5)
        obs["a_end_state"] = orch.workers[key][0].state
        obs["interrupt_detect_s"] = round(time.time() - t0, 1)
        # --- stream B comes back; watcher must reconnect
        for f in hls.glob("*"):
            f.unlink()
        current["p"] = HLSProducer(tmp / "b.mp4", hls, "b", live_forever=False)
        await asyncio.get_running_loop().run_in_executor(None, wait_playlist)
        t0 = time.time()
        while time.time() - t0 < 30:
            if await orch.watch_youtube_once(resolver=resolver):
                break
            await asyncio.sleep(1)
        obs["reconnected"] = orch.youtube_sessions[key]["reconnects"]
        obs["b_session"] = orch.workers[key][0].buffer.out_dir.name
        # --- watch B: buffer, moments, clips while live
        while current["p"].alive() or not orch.workers[key][1].done():
            w = orch.workers[key][0]
            existing = [s for s in w.buffer.segments if s.path.exists()]
            if existing:
                obs["buffer_samples"].append({"total": len(w.buffer.segments), "on_disk": len(existing),
                                              "span": existing[-1].end - existing[0].start, "pinned": bool(w.buffer.pins)})
            c = orch.db.one("SELECT id, status, updated_at FROM clips WHERE status='dry_run' ORDER BY id LIMIT 1")
            if c and "clip_ready_at" not in obs:
                obs["clip_ready_at"] = time.time()
                obs["clip_ready_while_live"] = current["p"].alive()
            await orch.watch_youtube_once(resolver=resolver)
            await asyncio.sleep(1)
        obs["b_ended_at"] = current["p"].ended_at
        # --- stream ended: watcher must close the session (no VOD fallback)
        for _ in range(20):
            await orch.watch_youtube_once(resolver=resolver)
            if key not in orch.youtube_sessions:
                break
            await asyncio.sleep(0.5)
        obs["session_closed"] = key not in orch.youtube_sessions
        for t in orch.workers.get(key, (None, None))[0].clip_tasks if key in orch.workers else []:
            await t
        await asyncio.sleep(0)

    asyncio.run(scenario())
    srv.shutdown()
    obs["moments"] = orch.db.query("SELECT * FROM moments")
    obs["clips"] = orch.db.query("SELECT * FROM clips")
    obs["events"] = [e["message"] for e in orch.db.query("SELECT message FROM events ORDER BY id")]
    obs["cfg"], obs["orch"] = cfg, orch
    return obs


def test_live_stream_connection(live_run):
    assert live_run["a_segments"] >= 3
    assert "receiving live video" in " ".join(live_run["events"])


def test_rolling_buffer_operation(live_run):
    s = live_run["buffer_samples"]
    assert s, "no buffer samples"
    assert max(x["total"] for x in s) > max(x["on_disk"] for x in s)       # old segments were pruned...
    unpinned = [x["span"] for x in s if not x["pinned"]]
    assert unpinned and max(unpinned) <= 20 + 4 * 2                          # ...keeping ~buffer_s of video,
    assert any(x["pinned"] and x["span"] > 20 + 4 for x in s)                # except footage pinned for a pending clip


def test_reconnection_after_interruption(live_run):
    assert live_run["a_end_state"] in ("ended", "stalled")
    assert live_run["interrupt_detect_s"] < 30
    assert live_run["reconnected"] == 1
    assert live_run["a_session"] != live_run["b_session"]                    # independent capture sessions
    ev = " ".join(live_run["events"])
    assert "capture interrupted" in ev and "reconnected (#1)" in ev


def test_moment_detection_live(live_run):
    m = live_run["moments"]
    assert len(m) == 1, m                       # the calm stream A produced nothing; B's event was found once
    sig = json.loads(m[0]["signals"])
    assert sig["session"] == live_run["b_session"] and 30 <= m[0]["t"] <= 46


def test_clip_extraction_while_live(live_run):
    clips = live_run["clips"]
    assert len(clips) == 1 and clips[0]["status"] == "dry_run", clips
    assert live_run["clip_ready_while_live"], "clip must be finished before the stream ends"
    assert live_run["clip_ready_at"] < live_run["b_ended_at"]


def test_caption_generation(live_run):
    from clipper.whisper_cli import captions_burned
    c = live_run["clips"][0]
    assert "NO WAY HE JUMPED" in Path(c["srt_path"]).read_text(encoding="utf-8")
    from clipper.whisper_cli import clip_credit
    ok, detail = captions_burned(Path(c["raw_path"]), Path(c["vertical_path"]), Path(c["srt_path"]), live_run["cfg"],
                                 credit=clip_credit(c["stream_key"]))
    assert ok, detail


def test_ended_stream_closes_session(live_run):
    assert live_run["session_closed"]
    assert "broadcast ended" in " ".join(live_run["events"])
