import asyncio
import time

import httpx

from clipper.capture import wait_for_segments
from clipper.config import load_config
from clipper.discovery.twitch import StreamInfo, TwitchAPI
from clipper.pipeline import Orchestrator


def live(login, game="Grand Theft Auto VI"):
    return StreamInfo("twitch", login, "t", 100, f"https://www.twitch.tv/{login}", channel_id="42", game=game)


class FakeAPI:
    def __init__(self, seq):
        self.seq = list(seq)

    def streams_by_login(self, logins):
        return self.seq.pop(0) if len(self.seq) > 1 else self.seq[0]


def make_orch(tmp_path, monkeypatch, run_seconds):
    cfg = load_config(overrides={
        "data_dir": str(tmp_path), "transcription": {"enabled": False},
        "authorized_channels": [{"platform": "twitch", "channel": "Good", "authorized": True},
                                {"platform": "twitch", "channel": "nope", "authorized": False}],
        "twitch_watch": {"min_backoff_s": 10, "max_backoff_s": 40, "healthy_after_s": 120}})
    orch = Orchestrator(cfg)
    runs = []

    async def fake_run(self):
        runs.append(self.spec.key)
        await asyncio.sleep(run_seconds)
    monkeypatch.setattr("clipper.pipeline.StreamWorker.run", fake_run)
    return orch, runs


def test_starts_when_live_and_restarts_after_interruption_with_backoff(tmp_path, monkeypatch):
    orch, runs = make_orch(tmp_path, monkeypatch, 0)
    clock = [1000.0]
    monkeypatch.setattr("clipper.pipeline.time.time", lambda: clock[0])
    api = FakeAPI([[], [live("good"), live("nope")]])

    async def go():
        assert await orch.watch_twitch_once(api) == []            # offline: nothing
        assert await orch.watch_twitch_once(api) == ["twitch:good"]  # live: start (unauthorized ignored)
        await asyncio.sleep(0.01)                                   # worker dies immediately
        clock[0] += 1
        assert await orch.watch_twitch_once(api) == []              # quick failure -> backoff 10 s
        clock[0] += 11
        assert await orch.watch_twitch_once(api) == ["twitch:good"]  # retry
        await asyncio.sleep(0.01)
        clock[0] += 1
        assert await orch.watch_twitch_once(api) == []              # 2nd quick failure -> 20 s
        clock[0] += 15
        assert await orch.watch_twitch_once(api) == []
        clock[0] += 6
        assert await orch.watch_twitch_once(api) == ["twitch:good"]
    asyncio.run(go())
    assert runs == ["twitch:good"] * 3


def test_healthy_run_resets_backoff_and_offline_clears(tmp_path, monkeypatch):
    orch, runs = make_orch(tmp_path, monkeypatch, 0)
    clock = [1000.0]
    monkeypatch.setattr("clipper.pipeline.time.time", lambda: clock[0])
    api = FakeAPI([[live("good")]])

    async def go():
        await orch.watch_twitch_once(api)
        await asyncio.sleep(0.01)
        clock[0] += 500                                             # it ran a long time, then dropped
        assert await orch.watch_twitch_once(api) == ["twitch:good"]  # immediate reconnect, no backoff
        await asyncio.sleep(0.01)
        api.seq = [[]]                                              # channel goes offline
        await orch.watch_twitch_once(api)
        assert "twitch:good" not in orch.workers
    asyncio.run(go())


def test_wrong_game_and_manual_stop_not_restarted(tmp_path, monkeypatch):
    orch, runs = make_orch(tmp_path, monkeypatch, 0)
    async def go():
        assert await orch.watch_twitch_once(FakeAPI([[live("good", game="Minecraft")]])) == []
        api = FakeAPI([[live("good")]])
        await orch.watch_twitch_once(api)
        await asyncio.sleep(0.01)
        orch.workers["twitch:good"][0].manual_stop = True
        assert await orch.watch_twitch_once(api) == []
    asyncio.run(go())


def test_poll_failure_is_logged_not_raised(tmp_path, monkeypatch):
    orch, _ = make_orch(tmp_path, monkeypatch, 0)

    class Boom:
        def streams_by_login(self, logins):
            raise httpx.ConnectError("down")
    assert asyncio.run(orch.watch_twitch_once(Boom())) == []
    assert orch.db.one("SELECT message FROM events WHERE source='twitch_watch'")["message"].startswith("poll failed")


def test_streams_by_login_and_token_refresh_on_401():
    tokens, calls = iter(["t1", "t2"]), []

    def h(req):
        if req.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": next(tokens), "expires_in": 3600})
        calls.append(req.headers["Authorization"])
        if req.headers["Authorization"] == "Bearer t1":
            return httpx.Response(401)
        assert req.url.params.get_list("user_login") == ["good", "other"]
        return httpx.Response(200, json={"data": [{"user_login": "good", "user_id": "42", "title": "x",
                                                   "viewer_count": 3, "game_name": "Grand Theft Auto VI"}]})
    api = TwitchAPI("c", "s", http=httpx.Client(transport=httpx.MockTransport(h)))
    res = api.streams_by_login(["good", "other"])
    assert res[0].game == "Grand Theft Auto VI" and calls == ["Bearer t1", "Bearer t2"]


def test_stalled_capture_is_killed():
    class Buf:
        running, stalled, stopped = True, False, False
        def poll_new(self):
            return []
        def stop(self):
            self.stopped, self.running = True, False

    async def go():
        b = Buf()
        t0 = time.monotonic()
        async for _ in wait_for_segments(b, poll_s=0.05, stall_s=0.3):
            pass
        return b, time.monotonic() - t0
    b, dt = asyncio.run(go())
    assert b.stalled and b.stopped and dt < 2
