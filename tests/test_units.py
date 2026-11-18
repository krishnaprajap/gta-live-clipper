import json
import time

import httpx
import pytest

from clipper.budget import Budget, BudgetExceeded
from clipper.config import load_config
from clipper.db import DB
from clipper.detect import MomentDetector
from clipper.discovery.ranker import select
from clipper.discovery.twitch import StreamInfo, TwitchAPI
from clipper.discovery.youtube import YouTubeAPI
from clipper.metadata import template_metadata
from clipper.publish.youtube import YouTubePublisher
from clipper.quality import overlap_ratio, similarity
from clipper.signals.chat import ChatWindow, parse_privmsg
from clipper.transcribe import Word, chunk_words, write_srt


def test_parse_privmsg():
    assert parse_privmsg(":a!a@a.tmi.twitch.tv PRIVMSG #chan :KEKW that was nuts") == "KEKW that was nuts"
    assert parse_privmsg("@badge-info=;color=#FF0000 :a!a@a PRIVMSG #chan :hi") == "hi"
    assert parse_privmsg(":tmi.twitch.tv 001 justinfan1 :Welcome") is None


def test_chat_window_hype_and_stats():
    w = ChatWindow(["kekw", "clip it", "💀"])
    t = 1000.0
    for i, m in enumerate(["hello", "KEKW", "CLIP IT NOW", "lol 💀", "WHAT IS HAPPENING", "ok"]):
        w.add(m, t + i)
    n, hype = w.stats(t, t + 5)
    assert n == 6 and hype == 4  # kekw, clip it, emoji, all-caps


def test_detector_triggers_on_spike_with_cooldown():
    d = MomentDetector(weights={"chat_rate": 1, "chat_hype": 1.2, "audio": 1, "scene": 0.4}, threshold=3, cooldown_s=60)
    t, fired = 0.0, []
    for i in range(30):
        t += 4
        spike = i in (20, 21, 24)
        f = d.features(4, 40 if spike else 3 + i % 2, 30 if spike else 0,
                       {"rms_db": -10 if spike else -40 + (i % 3), "scene_max": 20 if spike else 1})
        if d.update(t, f):
            fired.append(i)
    assert fired == [20]  # 21 and 24 suppressed by cooldown


def test_detector_quiet_stream_no_false_positive():
    d = MomentDetector(weights={"chat_rate": 1, "chat_hype": 1.2, "audio": 1, "scene": 0.4}, threshold=3)
    assert not any(d.update(i * 4.0, d.features(4, i % 3, 0, {"rms_db": -40 + (i % 4) * 0.5, "scene_max": 2}))
                   for i in range(60))


def test_budget_hard_cap(tmp_path):
    b = Budget(DB(tmp_path / "x.db"), ai_cap_inr=1.0, total_cap_inr=8000)
    b.charge("ai", 0.6)
    assert not b.can_spend("ai", 0.5)
    with pytest.raises(BudgetExceeded):
        b.charge("ai", 0.5)
    assert b.summary()["ai_spent_inr"] == 0.6


def test_similarity_and_overlap():
    a = ",".join(f"{x:016x}" for x in [0xFFFF0000FFFF0000, 0x0F0F0F0F0F0F0F0F, 0x123456789ABCDEF0, 0xAAAAAAAAAAAAAAAA])
    assert similarity(a, a) == 1.0
    b = ",".join(f"{x:016x}" for x in [0x0000FFFF0000FFFF, 0xF0F0F0F0F0F0F0F0, 0xEDCBA9876543210F, 0x5555555555555555])
    assert similarity(a, b) < 0.9  # dedupe threshold
    assert overlap_ratio((0, 30), (20, 50)) == pytest.approx(1 / 3)


def test_srt_chunks(tmp_path):
    words = [Word(i * 0.3, i * 0.3 + 0.25, w) for i, w in enumerate("one two three four five six".split())]
    chunks = chunk_words(words, max_words=4)
    assert [c.text for c in chunks] == ["one two three four", "five six"]
    srt = write_srt(chunks, tmp_path / "a.srt").read_text()
    assert "00:00:00,000 --> " in srt and "ONE TWO THREE FOUR" in srt


def test_template_metadata_has_credit_and_limits():
    m = template_metadata("Streamer", "twitch", "No way he survived that crash. Insane.", {"z": {"audio": 5}},
                          "Clip from Streamer's live stream on Twitch. Used with permission.", "https://twitch.tv/streamer")
    assert len(m.title) <= 100 and "GTA 6" in m.title
    assert "Used with permission" in m.description and "https://twitch.tv/streamer" in m.description
    assert "GTA 6" in m.tags and len(m.tags) <= 15


def test_ranker_only_captures_authorized():
    cfg = load_config(overrides={"authorized_channels": [{"platform": "twitch", "channel": "good", "authorized": True}],
                                 "discovery": {"min_viewers": 10, "languages": ["en"]}})
    streams = [StreamInfo("twitch", "big", "GTA 6 RP", 50000, "u", "en"),
               StreamInfo("twitch", "good", "GTA VI day 1", 300, "u", "en"),
               StreamInfo("twitch", "tiny", "gta 6", 2, "u", "en")]
    chosen, ranked = select(streams, cfg, 3)
    assert [s.channel for s in chosen] == ["good"]
    assert [s.channel for s in ranked] == ["big", "good"]


def mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_twitch_discovery_and_clip_requests():
    seen = []

    def h(req: httpx.Request):
        seen.append((req.method, req.url.path, dict(req.url.params)))
        if req.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "app", "expires_in": 3600})
        if req.url.path == "/helix/games":
            return httpx.Response(200, json={"data": [{"id": "999", "name": "Grand Theft Auto VI"}]})
        if req.url.path == "/helix/streams":
            assert req.headers["Authorization"] == "Bearer app" and req.headers["Client-Id"] == "cid"
            return httpx.Response(200, json={"data": [{"user_login": "x", "user_id": "1", "title": "GTA 6", "viewer_count": 5,
                                                       "language": "en", "started_at": ""}]})
        if req.url.path == "/helix/clips":
            assert req.headers["Authorization"] == "Bearer usertok"
            return httpx.Response(202, json={"data": [{"id": "Clip1", "edit_url": "https://clips.twitch.tv/Clip1/edit"}]})
        return httpx.Response(404)

    api = TwitchAPI("cid", "sec", http=mock_client(h))
    s = api.live_streams(["Grand Theft Auto VI"], ["en"])
    assert s[0].channel == "x" and s[0].url == "https://www.twitch.tv/x"
    assert ("GET", "/helix/streams", {"game_id": "999", "first": "100", "type": "live", "language": "en"}) in seen
    assert api.create_clip("1", "usertok")["id"] == "Clip1"


def test_youtube_discovery_counts_quota():
    used = []

    def h(req):
        if req.url.path.endswith("/search"):
            assert req.url.params["eventType"] == "live"
            return httpx.Response(200, json={"items": [{"id": {"videoId": "v1"}}, {"id": {"videoId": "v2"}}]})
        return httpx.Response(200, json={"items": [
            {"id": "v1", "snippet": {"channelTitle": "Ch", "title": "GTA 6 live", "channelId": "UC1"},
             "liveStreamingDetails": {"concurrentViewers": "120", "activeLiveChatId": "chat1"}},
            {"id": "v2", "snippet": {"channelTitle": "Old", "title": "x", "channelId": "UC2"},
             "liveStreamingDetails": {"actualEndTime": "2026-01-01T00:00:00Z"}}]})

    yt = YouTubeAPI("k", http=mock_client(h), on_quota=used.append)
    res = yt.live_streams(channel_ids=["UC1"])
    assert len(res) == 1 and res[0].live_chat_id == "chat1" and res[0].viewers == 120
    assert used == [100, 1]


def test_youtube_resumable_upload(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 25)
    calls = []

    def h(req):
        calls.append(req)
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        if req.method == "POST":
            body = json.loads(req.content)
            assert body["status"]["privacyStatus"] == "private" and body["snippet"]["categoryId"] == "20"
            return httpx.Response(200, headers={"Location": "https://upload.example/session"})
        rng = req.headers["Content-Range"]
        if rng.startswith("bytes 0-"):
            return httpx.Response(308, headers={"Range": "bytes=0-9"})
        return httpx.Response(200, json={"id": "VID123"})

    pub = YouTubePublisher("c", "s", "r", http=mock_client(h), chunk_mb=1)
    pub.chunk = 10
    res = pub.publish(video, {"title": "t", "description": "d", "tags": ["a"]})
    assert res.ok and res.remote_id == "VID123"
    puts = [c.headers["Content-Range"] for c in calls if c.method == "PUT"]
    assert puts == ["bytes 0-9/25", "bytes 10-19/25"]


def test_youtube_upload_quota_error_is_reported(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")

    def h(req):
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at"})
        return httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}})

    res = YouTubePublisher("c", "s", "r", http=mock_client(h)).publish(video, {"title": "t", "description": "d"})
    assert not res.ok and "HTTP 403" in res.error and "quotaExceeded" in res.error
