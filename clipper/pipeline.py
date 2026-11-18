"""Orchestration: per-stream workers (capture -> signals -> detection ->
extraction) feeding one bounded clip-processing queue (quality -> dedupe ->
transcribe -> vertical edit -> metadata -> publish)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .budget import Budget
from .capture import RollingBuffer, wait_for_segments
from .config import Config, secret
from .db import DB
from .detect import MomentDetector
from .edit import make_vertical
from .ffmpeg_utils import analyze_segment
from .metadata import generate as generate_metadata
from .publish import make_publisher
from .quality import check_quality, fingerprint, overlap_ratio, similarity
from .signals.chat import ChatWindow, ReplayChat, TwitchChat, YouTubeChat
from .storage import cleanup
from .transcribe import TranscriptionUnavailable, Transcriber, chunk_words, write_srt

log = logging.getLogger("clipper")


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s)[:40] or "stream"


@dataclass
class StreamSpec:
    platform: str            # twitch | youtube | file
    channel: str
    source: str              # URL for streamlink or path for file simulation
    channel_id: str = ""
    live_chat_id: str = ""
    chat_replay: str = ""    # JSONL file for offline tests

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.channel}"


def _lower_priority(nice: int = 10) -> None:
    try:
        import os
        import threading
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), nice)
    except (AttributeError, OSError):
        pass


class ClipProcessor:
    """CPU-heavy work runs on a single worker thread, so N streams never spawn
    N concurrent encodes. Capture/analysis keep running meanwhile."""

    def __init__(self, cfg: Config, db: DB, budget: Budget):
        self.cfg, self.db, self.budget = cfg, db, budget
        self.data_dir = cfg.data_dir
        self.clips_dir = self.data_dir / "clips"
        self.clips_dir.mkdir(exist_ok=True)
        tcfg = cfg["transcription"]
        self.transcriber = Transcriber(tcfg["model"], tcfg["compute_type"], tcfg.get("language"),
                                       cpu_threads=int(tcfg.get("cpu_threads", 2))) if tcfg["enabled"] else None
        self.publisher = make_publisher(cfg, self.data_dir)
        # Clip work (transcode/transcribe) runs at lower CPU priority so live
        # capture + analysis never fall behind on a small VPS. On Linux the
        # nice value is per-thread and inherited by spawned ffmpeg processes.
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clipwork", initializer=_lower_priority)

    def uploads_today(self) -> int:
        since = time.time() - 86400
        return self.db.one("SELECT COUNT(*) AS n FROM publish_jobs WHERE status='published' AND created_at>=?",
                           (since,))["n"]

    def is_duplicate(self, clip_id: int, stream_key: str, window: tuple[float, float], fp: str,
                     session: str | None = None) -> str | None:
        """Window overlap is only meaningful inside one capture session: capture
        time restarts at 0 on every (re)connect. Across sessions, the visual
        fingerprint catches real duplicates."""
        for r in self.db.query("SELECT c.id, c.stream_key, c.fingerprint, m.t, m.signals FROM clips c JOIN moments m "
                               "ON m.id=c.moment_id WHERE c.id!=? AND c.created_at>=? AND c.status NOT IN "
                               "('rejected','duplicate','failed')", (clip_id, time.time() - 6 * 3600)):
            sig = json.loads(r["signals"] or "{}")
            if r["stream_key"] == stream_key and "window" in sig and sig.get("session") == session:
                if overlap_ratio(window, tuple(sig["window"])) > 0.5:
                    return f"overlaps clip {r['id']}"
            if fp and r["fingerprint"] and similarity(fp, r["fingerprint"]) > 0.9:
                return f"visually matches clip {r['id']}"
        return None

    def process(self, clip_id: int) -> dict:
        db = self.db
        clip = db.one("SELECT * FROM clips WHERE id=?", (clip_id,))
        moment = db.one("SELECT * FROM moments WHERE id=?", (clip["moment_id"],))
        signals = json.loads(moment["signals"] or "{}")
        platform, channel = clip["stream_key"].split(":", 1)
        raw = Path(clip["raw_path"])
        steps: dict = {}
        try:
            q = check_quality(raw)
            steps["quality"] = q.metrics
            if not q.ok:
                db.update("clips", clip_id, status="rejected", error=q.reason, duration=q.duration)
                db.log("info", "quality", f"clip {clip_id} rejected: {q.reason}")
                return {"status": "rejected", "reason": q.reason}
            fp = fingerprint(raw)
            dup = self.is_duplicate(clip_id, clip["stream_key"], tuple(signals.get("window", (0, 0))), fp,
                                    signals.get("session"))
            db.update("clips", clip_id, fingerprint=fp, duration=q.duration)
            if dup:
                db.update("clips", clip_id, status="duplicate", error=dup)
                return {"status": "duplicate", "reason": dup}

            # transcription: never skipped silently. If Whisper fails the clip is
            # still edited (without captions) but marked caption_failed, logged as
            # an error and held back from publishing until it is reviewed.
            transcript, srt, caption_error = "", None, None
            if self.transcriber:
                db.update("clips", clip_id, status="transcribing")
                try:
                    words, lang = self.transcriber.words(raw)
                    transcript = " ".join(w.text for w in words)
                    if words:
                        srt = write_srt(chunk_words(words), raw.with_suffix(".srt"))
                    else:
                        db.log("info", "transcribe", f"clip {clip_id}: no speech detected (no captions needed)")
                    steps["transcription"] = {"words": len(words), "language": lang}
                except Exception as e:  # model missing/corrupt, decode error, out of memory...
                    caption_error = f"captions failed: {e}"[:500]
                    steps["transcription"] = {"error": str(e)}
                    db.log("error", "transcribe", f"clip {clip_id}: {caption_error}. Fix: python -m clipper whisper repair")

            db.update("clips", clip_id, status="editing", transcript=transcript, srt_path=str(srt) if srt else None)
            ecfg = self.cfg["edit"]
            vertical = raw.with_name(raw.stem + "_vertical.mp4")
            from .whisper_cli import clip_credit
            make_vertical(raw, vertical, srt=srt, credit=clip_credit(clip["stream_key"]), width=ecfg["width"],
                          height=ecfg["height"], crf=ecfg["crf"], preset=ecfg["preset"],
                          max_duration=ecfg["max_duration_s"], threads=ecfg.get("threads", 2))

            stream = db.one("SELECT url FROM streams WHERE platform=? AND channel=?", (platform, channel)) or {}
            meta = generate_metadata(self.cfg, self.budget, channel, platform, transcript, signals,
                                     source_url=u if (u := stream.get("url") or "").startswith("http") else "", seed=clip_id)
            db.update("clips", clip_id, vertical_path=str(vertical), title=meta.title, description=meta.description,
                      tags=json.dumps(meta.tags), status="ready")
            steps["metadata"] = meta.source
            if caption_error and self.cfg["transcription"].get("required", True):
                db.update("clips", clip_id, status="caption_failed", error=caption_error)
                db.execute("INSERT INTO publish_jobs(clip_id,target,status,error,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                           (clip_id, self.publisher.name, "held", caption_error, time.time(), time.time()))
                steps["publish"] = {"status": "held", "reason": caption_error}
                return {"status": "caption_failed", **steps}
            steps["publish"] = self.publish(clip_id, vertical, meta.as_dict())
            return {"status": "done", **steps}
        except Exception as e:  # keep the worker alive; record the error
            log.exception("clip %s failed", clip_id)
            db.update("clips", clip_id, status="failed", error=f"{type(e).__name__}: {e}"[:1000])
            db.log("error", "process", f"clip {clip_id}: {e}")
            return {"status": "failed", "error": str(e)}

    def publish(self, clip_id: int, video: Path, meta: dict) -> dict:
        db = self.db
        target = self.publisher.name
        job = db.execute("INSERT INTO publish_jobs(clip_id,target,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                         (clip_id, target, "pending", time.time(), time.time()))
        if target != "dry_run":
            platform, ids = self._channel_ids(clip_id)
            if not self.cfg.publish_allowed(platform, ids):
                reason = (f"publishing not authorized for {platform}:{'/'.join(ids)}; needs authorized: true AND "
                          f"publish_allowed: true on its authorized_channels entry")
                db.update("publish_jobs", job, status="held", error=reason)
                db.update("clips", clip_id, status="held", error=reason)
                db.log("warning", "publish", f"clip {clip_id}: {reason}")
                return {"status": "held", "reason": reason}
        if target != "dry_run" and self.uploads_today() >= self.cfg["publish"]["max_uploads_per_day"]:
            db.update("publish_jobs", job, status="skipped", error="daily upload cap reached")
            return {"status": "skipped", "reason": "daily cap"}
        res = None
        for attempt in range(1, 4):
            res = self.publisher.publish(video, meta)
            db.update("publish_jobs", job, status=res.status, remote_id=res.remote_id, remote_url=res.remote_url,
                      error=res.error, attempts=attempt)
            if res.ok or "HTTP 4" in res.error:  # don't retry client errors (quota, auth, policy)
                break
            time.sleep(5 * attempt)
        db.update("clips", clip_id, status="published" if res.status == "published" else
                  ("dry_run" if res.status == "dry_run" else "publish_failed"), error=res.error or None)
        db.log("info" if res.ok else "error", "publish", f"clip {clip_id} -> {target}: {res.status} {res.remote_url} {res.error}")
        return {"status": res.status, "url": res.remote_url, "error": res.error}

    def _channel_ids(self, clip_id: int) -> tuple[str, list[str]]:
        r = self.db.one("SELECT c.stream_key, m.signals FROM clips c JOIN moments m ON m.id=c.moment_id WHERE c.id=?", (clip_id,))
        if not r:
            return "", []
        platform, channel = r["stream_key"].split(":", 1)
        ids = json.loads(r["signals"] or "{}").get("channel_ids") or []
        return platform, list(dict.fromkeys(ids + [channel]))

    async def submit(self, clip_id: int) -> dict:
        return await asyncio.get_running_loop().run_in_executor(self.pool, self.process, clip_id)


@dataclass
class StreamWorker:
    spec: StreamSpec
    cfg: Config
    db: DB
    processor: ClipProcessor
    analysis_pool: ThreadPoolExecutor
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: list = field(default_factory=list)
    clip_tasks: list = field(default_factory=list)
    twitch_api: object = None
    started: float = 0.0
    manual_stop: bool = False
    state: str = "connecting"        # connecting | capturing | ended | stalled | error | stopped
    last_error: str | None = None

    def __post_init__(self):
        c, d = self.cfg["capture"], self.cfg["detection"]
        self.buffer = RollingBuffer(key=self.spec.key, out_dir=self.cfg.data_dir / "buffers" / f"{safe_name(self.spec.key)}_{int(time.time())}",
                                    source=self.spec.source, quality=c["quality"], segment_s=c["segment_s"],
                                    buffer_s=c["buffer_s"], simulate_file=self.spec.platform == "file",
                                    stream_timeout_s=float(c.get("stream_timeout_s", 60)))
        self.chat_window = ChatWindow(d["hype_terms"])
        weights = dict(d["weights"])
        sp = d.get("speech_signal") or {}
        self.speech = None
        if sp.get("enabled"):
            from .signals.speech import SpeechSignal
            self.speech = SpeechSignal(d["hype_terms"], sp.get("model", "tiny"), int(sp.get("threads", 1)))
            weights.setdefault("speech", float(sp.get("weight", 1.0)))
        self.detector = MomentDetector(weights=weights, threshold=d["threshold"], cooldown_s=d["cooldown_s"])
        self.chat = self._make_chat()
        self.offsets: list[float] = []
        self.pending: list[dict] = []

    def _make_chat(self):
        s = self.spec
        if s.chat_replay:
            return ReplayChat(s.chat_replay, self.chat_window)
        if s.platform == "twitch":
            return TwitchChat(s.channel, self.chat_window)
        if s.platform == "youtube" and s.live_chat_id and secret("YOUTUBE_API_KEY"):
            return YouTubeChat(s.live_chat_id, secret("YOUTUBE_API_KEY"), self.chat_window)
        return None

    async def run(self) -> None:
        key = self.spec.key
        self.db.upsert_stream(self.spec.platform, self.spec.channel, status="capturing", url=self.spec.source,
                              started_at=time.time(), error=None)
        self.db.log("info", key, "capture started")
        self.buffer.start()
        self.tasks.append(asyncio.create_task(self._stop_watch()))
        if self.chat:
            self.tasks.append(asyncio.create_task(self.chat.run(self.stop)))
        loop = asyncio.get_running_loop()
        d, c = self.cfg["detection"], self.cfg["capture"]
        try:
            stall = None if self.spec.platform == "file" else float(c.get("stall_timeout_s", 300))
            async for seg in wait_for_segments(self.buffer, stall_s=stall):
                if self.stop.is_set():
                    break
                self.offsets = (self.offsets + [seg.wall_end - seg.end])[-10:]
                off = statistics.median(self.offsets) + float(d.get("chat_delay_s", 0))
                count, hype = self.chat_window.stats(seg.start + off, seg.end + off)
                if self.state == "connecting":
                    self.state = "capturing"
                    self.db.log("info", key, "receiving live video")
                analysis = await loop.run_in_executor(self.analysis_pool, analyze_segment, seg.path)
                feats = self.detector.features(seg.duration, count, hype, analysis)
                if self.speech:
                    sv = await loop.run_in_executor(self.analysis_pool, self.speech.score, str(seg.path), seg.duration)
                    if sv is not None:
                        feats["speech"] = sv
                det = self.detector.update(seg.end, feats)
                self._extend_pending(seg.end, self.detector.history[-1]["score"])
                if det:
                    await self._on_detection(det)
                await self._flush_ready()
            # stream ended: extract whatever is still pending with what we have
            # Clip processing continues in the background (the orchestrator
            # keeps the tasks), so a reconnect is not delayed by encoding.
            await self._flush_ready(force=True)
            status, err = ("stalled", "no data for stall_timeout_s; reconnecting") if self.buffer.stalled else ("ended", None)
            if self.manual_stop:
                status, err = "stopped", None
            if not self.buffer.segments:
                status, err = "error", self.buffer.tail_log()
        except asyncio.CancelledError:
            status, err = "stopped", None
        finally:
            self.stop.set()
            self.buffer.stop()
            for t in self.tasks:
                t.cancel()
        self.buffer.cleanup()  # pending clips were extracted above
        self.state, self.last_error = status, err
        self.db.upsert_stream(self.spec.platform, self.spec.channel, status=status, error=err)
        self.db.log("info" if status != "error" else "error", key, f"capture {status}" + (f": {err[-300:]}" if err else ""))

    async def _stop_watch(self) -> None:
        """STOP must work even while no segment arrives (connecting/stalled)."""
        await self.stop.wait()
        await asyncio.get_running_loop().run_in_executor(None, self.buffer.stop)

    def _extend_pending(self, t: float, score: float) -> None:
        for p in self.pending:
            if t - p["peak"] <= self.cfg["detection"]["post_roll_s"] and score >= p["score"]:
                p["peak"], p["score"] = t, score

    async def _on_detection(self, det) -> None:
        d = self.cfg["detection"]
        moment_id = self.db.add_moment(self.spec.key, det.t, det.score, det.signals, "pending")
        pin = self.buffer.pin(det.t - d["pre_roll_s"] - 10, det.t + d["post_roll_s"] + 30)
        self.pending.append({"moment": moment_id, "peak": det.t, "first": det.t, "score": det.score, "pin": pin})
        self.db.log("info", self.spec.key, f"moment {moment_id} detected at {det.t:.1f}s score={det.score:.2f}")
        if (self.cfg["publish"]["mode"] == "twitch_clip" and self.spec.platform == "twitch"
                and self.cfg.publish_allowed("twitch", [self.spec.channel])):
            await self._twitch_native_clip(moment_id)

    async def _twitch_native_clip(self, moment_id: int) -> None:
        token, cid, csec = secret("TWITCH_USER_TOKEN"), secret("TWITCH_CLIENT_ID"), secret("TWITCH_CLIENT_SECRET")
        if not (token and cid and csec and self.spec.channel_id):
            self.db.log("warning", "twitch_clip", "missing TWITCH_USER_TOKEN/client creds/broadcaster id")
            return
        from .discovery.twitch import TwitchAPI
        try:
            api = self.twitch_api or TwitchAPI(cid, csec)
            res = await asyncio.get_running_loop().run_in_executor(None, api.create_clip, self.spec.channel_id, token)
            self.db.log("info", "twitch_clip", f"moment {moment_id}: created Twitch clip {res.get('edit_url')}")
        except Exception as e:
            self.db.log("error", "twitch_clip", f"moment {moment_id}: {e}")

    async def _flush_ready(self, force: bool = False) -> None:
        d = self.cfg["detection"]
        head = self.buffer.head
        for p in list(self.pending):
            if not force and head < p["peak"] + d["post_roll_s"]:
                continue
            self.pending.remove(p)
            min_free = float(self.cfg["storage"].get("min_free_gb", 0))
            if min_free and shutil.disk_usage(self.cfg.data_dir).free / 1024 ** 3 < min_free:
                self.db.update("moments", p["moment"], status="skipped", reason=f"free disk below {min_free} GB")
                self.db.log("warning", "storage", f"moment {p['moment']} skipped: free disk below {min_free} GB")
                self.buffer.unpin(p["pin"])
                continue
            start = max(0.0, p["first"] - d["pre_roll_s"])
            end = min(p["peak"] + d["post_roll_s"], start + self.cfg["edit"]["max_duration_s"])
            raw = self.processor.clips_dir / f"{safe_name(self.spec.key)}_{int(time.time())}_{p['moment']}.mp4"
            try:
                await asyncio.get_running_loop().run_in_executor(self.analysis_pool, self.buffer.extract, start, end, raw)
            except Exception as e:
                self.db.update("moments", p["moment"], status="extract_failed", reason=str(e)[:500])
                self.buffer.unpin(p["pin"])
                continue
            self.buffer.unpin(p["pin"])
            sig = json.loads(self.db.one("SELECT signals FROM moments WHERE id=?", (p["moment"],))["signals"])
            sig["window"] = [start, end]
            sig["session"] = self.buffer.out_dir.name  # unique per capture session
            sig["channel_ids"] = [x for x in (self.spec.channel_id, self.spec.channel) if x]
            self.db.execute("UPDATE moments SET status='extracted', signals=? WHERE id=?", (json.dumps(sig), p["moment"]))
            now = time.time()
            clip_id = self.db.execute("INSERT INTO clips(moment_id,stream_key,raw_path,status,created_at,updated_at) "
                                      "VALUES(?,?,?,?,?,?)", (p["moment"], self.spec.key, str(raw), "extracted", now, now))
            self.clip_tasks.append(asyncio.create_task(self.processor.submit(clip_id)))


class Orchestrator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = DB(cfg.data_dir / "clipper.db")
        b = cfg["budget"]
        self.budget = Budget(self.db, b["ai_monthly_cap_inr"], b["monthly_total_inr"])
        self.processor = ClipProcessor(cfg, self.db, self.budget)
        self.analysis_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="analysis")
        self.workers: dict[str, tuple[StreamWorker, asyncio.Task]] = {}
        self.discovered: list[dict] = []

    def _active_count(self) -> int:
        return sum(1 for _, t in self.workers.values() if not t.done())

    def start_stream(self, spec: StreamSpec) -> StreamWorker:
        old = self.workers.get(spec.key)
        if old and not old[1].done():
            return old[0]
        if spec.platform in ("twitch", "youtube") and not self.cfg.authorized(spec.platform, spec.channel_id or spec.channel) \
                and not self.cfg.authorized(spec.platform, spec.channel):
            raise PermissionError(f"{spec.key} is not on the authorized_channels allowlist")
        w = StreamWorker(spec, self.cfg, self.db, self.processor, self.analysis_pool)
        w.started = time.time()
        self.workers[spec.key] = (w, asyncio.create_task(w.run()))
        return w

    async def run_single(self, spec: StreamSpec) -> None:
        w = self.start_stream(spec)
        await self.workers[spec.key][1]
        if w.clip_tasks:
            await asyncio.gather(*w.clip_tasks)

    def _discover(self):
        from .discovery.ranker import select
        found = []
        dcfg = self.cfg["discovery"]
        if secret("TWITCH_CLIENT_ID") and secret("TWITCH_CLIENT_SECRET"):
            from .discovery.twitch import TwitchAPI
            try:
                found += TwitchAPI(secret("TWITCH_CLIENT_ID"), secret("TWITCH_CLIENT_SECRET")).live_streams(
                    dcfg["game_names"], dcfg.get("languages"))
            except Exception as e:
                self.db.log("error", "discovery.twitch", str(e))
        if secret("YOUTUBE_API_KEY"):
            from .discovery.youtube import YouTubeAPI
            yt_ids = [c["channel"] for c in self.cfg["authorized_channels"] if c["platform"] == "youtube" and c.get("authorized")]
            if yt_ids:  # only search authorized channels: keeps quota use bounded (100 units each)
                try:
                    found += YouTubeAPI(secret("YOUTUBE_API_KEY")).live_streams("GTA 6", channel_ids=yt_ids)
                except Exception as e:
                    self.db.log("error", "discovery.youtube", str(e))
        return select(found, self.cfg, self.cfg.max_streams)

    # -- Twitch watchdog: start/restart authorized channels as soon as they are live
    def _twitch_api(self):
        if not hasattr(self, "_tw_api"):
            from .discovery.twitch import TwitchAPI
            self._tw_api = TwitchAPI(secret("TWITCH_CLIENT_ID"), secret("TWITCH_CLIENT_SECRET"))
        return self._tw_api

    async def watch_twitch_once(self, api=None) -> list[str]:
        """One poll. Returns the keys started. Backoff after quick failures:
        min_backoff_s * 2^(n-1), capped at max_backoff_s."""
        wcfg = self.cfg.get("twitch_watch", {})
        logins = [c["channel"].lower() for c in self.cfg["authorized_channels"]
                  if c.get("platform") == "twitch" and c.get("authorized")]
        if not logins:
            return []
        api = api or self._twitch_api()
        try:
            live = await asyncio.get_running_loop().run_in_executor(None, api.streams_by_login, logins)
        except Exception as e:
            self.db.log("warning", "twitch_watch", f"poll failed: {e}")
            return []
        games = {g.lower() for g in self.cfg["discovery"]["game_names"]}
        now, started = time.time(), []
        st = self.__dict__.setdefault("_watch_state", {})
        live_keys = {f"twitch:{s.channel}" for s in live}
        for key, (w, t) in list(self.workers.items()):  # channel went offline: forget finished worker + backoff
            if key.startswith("twitch:") and key not in live_keys and t.done():
                self.workers.pop(key)
                st.pop(key, None)
        for s in live:
            key = f"twitch:{s.channel}"
            if wcfg.get("require_game", True) and s.game.lower() not in games:
                continue
            entry = self.workers.get(key)
            if entry and (not entry[1].done() or entry[0].manual_stop):
                continue  # running, or stopped by the operator (stays stopped until the channel goes offline)
            state = st.setdefault(key, {"fails": 0, "next": 0.0})
            if entry:  # worker finished while the channel is still live -> interruption
                w = entry[0]
                lived = now - (w.started or now)
                state["fails"] = 0 if lived > wcfg.get("healthy_after_s", 120) else state["fails"] + 1
                if state["fails"]:
                    delay = min(wcfg.get("min_backoff_s", 10) * 2 ** (state["fails"] - 1), wcfg.get("max_backoff_s", 600))
                    state["next"] = now + delay
                    self.db.log("warning", "twitch_watch", f"{key} interrupted; retry #{state['fails']} in {delay:.0f}s")
                self.workers.pop(key)
            if now < state["next"] or self._active_count() >= self.cfg.max_streams:
                continue
            try:
                self.start_stream(StreamSpec("twitch", s.channel, s.url, channel_id=s.channel_id))
                started.append(key)
                self.db.upsert_stream("twitch", s.channel, title=s.title, viewers=s.viewers)
                self.db.log("info", "twitch_watch", f"{key} live ({s.game}); capture (re)started")
            except PermissionError as e:
                self.db.log("warning", "twitch_watch", str(e))
        return started

    async def watch_twitch_forever(self) -> None:
        poll = float(self.cfg.get("twitch_watch", {}).get("poll_s", 30))
        while True:
            await self.watch_twitch_once()
            await asyncio.sleep(poll)

    # -- YouTube Live sessions started from the dashboard / CLI ----------------
    def validate_youtube(self, url: str, resolver=None):
        """Blocking: URL check -> live check -> allowlist. Returns (info, ok, http_status, message)."""
        from .youtube_live import authorization, resolve
        info = (resolver or (lambda u: resolve(u, secret("YOUTUBE_API_KEY"))))(url)
        if info.status == "invalid":
            return info, False, 400, info.message
        if info.status == "error":
            return info, False, 502, f"{info.message}: {info.raw_error}".strip(": ")
        if not info.live:
            return info, False, 409, info.message or "not live"
        ok, msg = authorization(self.cfg, info)
        if not ok:
            return info, False, 403, msg
        return info, True, 200, msg

    def launch_youtube(self, info, url: str) -> str:
        """Must run on the event loop. Starts an independent capture session."""
        channel = info.handle or info.channel_id or info.video_id
        spec = StreamSpec("youtube", channel, info.watch_url, channel_id=info.channel_id, live_chat_id=info.live_chat_id)
        sessions = self.__dict__.setdefault("youtube_sessions", {})
        sess = sessions.get(spec.key)
        if sess and spec.key in self.workers and not self.workers[spec.key][1].done():
            return spec.key  # already capturing
        self.start_stream(spec)
        sessions[spec.key] = {"url": url, "watch_url": info.watch_url, "title": info.title, "video_id": info.video_id,
                              "channel": channel, "channel_id": info.channel_id, "state": "capturing", "fails": 0,
                              "next": 0.0, "reconnects": 0, "started": time.time(), "message": "", "spec": spec}
        self.db.upsert_stream("youtube", channel, title=info.title, viewers=info.viewers, url=info.watch_url)
        self.db.log("info", spec.key, f"YouTube Live capture started: {info.title!r} ({info.watch_url}, check via {info.method})")
        if not info.live_chat_id:
            self.db.log("info", spec.key, "live chat signal off (needs YOUTUBE_API_KEY); using audio + video signals")
        return spec.key

    async def watch_youtube_once(self, resolver=None) -> list[str]:
        """Reconnect interrupted sessions while the stream is still live; end
        sessions whose broadcast has ended. Never falls back to VOD."""
        from .youtube_live import resolve
        wcfg = self.cfg.get("youtube_watch", {})
        resolver = resolver or (lambda u: resolve(u, secret("YOUTUBE_API_KEY")))
        sessions = self.__dict__.setdefault("youtube_sessions", {})
        now, restarted = time.time(), []
        for key, sess in list(sessions.items()):
            entry = self.workers.get(key)
            if entry and not entry[1].done():
                sess["state"] = entry[0].state
                continue
            if entry and entry[0].manual_stop:
                sess["state"] = "stopped"
                self.db.log("info", key, "capture stopped by user")
                sessions.pop(key)
                continue
            if entry and sess.get("handled") is not entry[0]:  # first look at this worker's exit
                lived = now - (entry[0].started or now)
                sess["fails"] = 0 if lived > wcfg.get("healthy_after_s", 120) else sess["fails"] + 1
                delay = 0 if not sess["fails"] else min(wcfg.get("min_backoff_s", 10) * 2 ** (sess["fails"] - 1),
                                                         wcfg.get("max_backoff_s", 300))
                sess["next"], sess["handled"] = now + delay, entry[0]
                sess["state"] = "reconnecting"
                sess["message"] = f"capture interrupted ({entry[0].state}: {(entry[0].last_error or '')[-200:]})".strip()
                self.db.log("warning", key, sess["message"] + (f"; checking stream in {delay:.0f}s" if delay else "; checking stream"))
            if now < sess["next"]:
                continue
            info = await asyncio.get_running_loop().run_in_executor(None, resolver, sess["watch_url"])
            if info.live:
                if self._active_count() >= self.max_streams_for_youtube():
                    continue
                self.workers.pop(key, None)
                self.start_stream(sess["spec"])
                sess["reconnects"] += 1
                sess["state"] = "capturing"
                restarted.append(key)
                self.db.log("info", key, f"stream still live; reconnected (#{sess['reconnects']})")
            elif info.status in ("not_live", "invalid", "upcoming"):
                sess["state"] = "ended"
                self.db.log("info", key, f"broadcast ended: {info.message or info.status}; session closed (no VOD download)")
                self.db.upsert_stream("youtube", sess["channel"], status="ended")
                self.workers.pop(key, None)
                sessions.pop(key)
            else:  # YouTube/streamlink error: report it verbatim, retry with backoff
                sess["fails"] += 1
                if sess["fails"] > wcfg.get("max_failures", 10):
                    sess["state"] = "error"
                    self.db.log("error", key, f"giving up after {sess['fails']} failed checks: {info.message} {info.raw_error}")
                    self.db.upsert_stream("youtube", sess["channel"], status="error", error=info.raw_error[-500:])
                    self.workers.pop(key, None)
                    sessions.pop(key)
                    continue
                delay = min(wcfg.get("min_backoff_s", 10) * 2 ** (sess["fails"] - 1), wcfg.get("max_backoff_s", 300))
                sess["next"] = now + delay
                sess["state"], sess["message"] = "reconnecting", f"{info.message}: {info.raw_error[-200:]}"
                self.db.log("error", key, f"live check failed ({sess['message']}); retry in {delay:.0f}s")
        return restarted

    def max_streams_for_youtube(self) -> int:
        return self.cfg.max_streams

    async def watch_youtube_forever(self) -> None:
        poll = float(self.cfg.get("youtube_watch", {}).get("poll_s", 15))
        while True:
            try:
                await self.watch_youtube_once()
            except Exception as e:  # keep watching other sessions
                self.db.log("error", "youtube_watch", f"{type(e).__name__}: {e}")
            await asyncio.sleep(poll)

    async def run_forever(self) -> None:
        self._yt_task = asyncio.create_task(self.watch_youtube_forever())
        dcfg, scfg = self.cfg["discovery"], self.cfg["storage"]
        if secret("TWITCH_CLIENT_ID") and secret("TWITCH_CLIENT_SECRET") and self.cfg.get("twitch_watch", {}).get("enabled", True):
            self._watch_task = asyncio.create_task(self.watch_twitch_forever())
        else:
            self.db.log("warning", "twitch_watch", "disabled: TWITCH_CLIENT_ID/SECRET not set; no auto-reconnect")
        last_cleanup = 0.0
        while True:
            for key, (w, t) in list(self.workers.items()):
                if t.done() and not (key.startswith("twitch:") and hasattr(self, "_watch_task")) \
                        and key not in self.__dict__.get("youtube_sessions", {}):
                    self.workers.pop(key)
            if time.time() - last_cleanup > 600:
                res = cleanup(self.db, self.cfg.data_dir, scfg["max_disk_gb"], scfg["keep_clips_days"], scfg["keep_published_days"])
                self.db.log("info", "storage", json.dumps(res))
                last_cleanup = time.time()
            chosen, ranked = await asyncio.get_running_loop().run_in_executor(None, self._discover)
            self.discovered = [vars(s) for s in ranked[:50]]
            for s in ranked[:50]:
                if f"{s.platform}:{s.channel}" not in self.workers:
                    self.db.upsert_stream(s.platform, s.channel, title=s.title, viewers=s.viewers, url=s.url,
                                          status="live" if s.authorized else "discovered_unauthorized")
            for s in chosen:
                key = f"{s.platform}:{s.channel}"
                if key in self.workers or self._active_count() >= self.cfg.max_streams:
                    continue
                spec = StreamSpec(s.platform, s.channel, s.url, channel_id=s.channel_id, live_chat_id=s.live_chat_id)
                try:
                    self.start_stream(spec)
                except PermissionError as e:
                    self.db.log("warning", "orchestrator", str(e))
            await asyncio.sleep(dcfg["interval_s"])
