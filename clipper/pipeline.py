"""Orchestration: per-stream workers (capture -> signals -> detection ->
extraction) feeding one bounded clip-processing queue (quality -> dedupe ->
transcribe -> vertical edit -> metadata -> publish)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
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
        self.transcriber = Transcriber(tcfg["model"], tcfg["compute_type"], tcfg.get("language")) if tcfg["enabled"] else None
        self.publisher = make_publisher(cfg, self.data_dir)
        # Clip work (transcode/transcribe) runs at lower CPU priority so live
        # capture + analysis never fall behind on a small VPS. On Linux the
        # nice value is per-thread and inherited by spawned ffmpeg processes.
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clipwork", initializer=_lower_priority)

    def uploads_today(self) -> int:
        since = time.time() - 86400
        return self.db.one("SELECT COUNT(*) AS n FROM publish_jobs WHERE status='published' AND created_at>=?",
                           (since,))["n"]

    def is_duplicate(self, clip_id: int, stream_key: str, window: tuple[float, float], fp: str) -> str | None:
        for r in self.db.query("SELECT c.id, c.stream_key, c.fingerprint, m.t, m.signals FROM clips c JOIN moments m "
                               "ON m.id=c.moment_id WHERE c.id!=? AND c.created_at>=? AND c.status NOT IN "
                               "('rejected','duplicate','failed')", (clip_id, time.time() - 6 * 3600)):
            sig = json.loads(r["signals"] or "{}")
            if r["stream_key"] == stream_key and "window" in sig:
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
            dup = self.is_duplicate(clip_id, clip["stream_key"], tuple(signals.get("window", (0, 0))), fp)
            db.update("clips", clip_id, fingerprint=fp, duration=q.duration)
            if dup:
                db.update("clips", clip_id, status="duplicate", error=dup)
                return {"status": "duplicate", "reason": dup}

            # transcription (optional; failures don't block the clip)
            transcript, srt = "", None
            if self.transcriber:
                db.update("clips", clip_id, status="transcribing")
                try:
                    words, lang = self.transcriber.words(raw)
                    transcript = " ".join(w.text for w in words)
                    if words:
                        srt = write_srt(chunk_words(words), raw.with_suffix(".srt"))
                    steps["transcription"] = {"words": len(words), "language": lang}
                except TranscriptionUnavailable as e:
                    steps["transcription"] = {"error": str(e)}
                    db.log("warning", "transcribe", str(e))

            db.update("clips", clip_id, status="editing", transcript=transcript, srt_path=str(srt) if srt else None)
            ecfg = self.cfg["edit"]
            vertical = raw.with_name(raw.stem + "_vertical.mp4")
            make_vertical(raw, vertical, srt=srt, credit=f"{channel} • {platform.title()}", width=ecfg["width"],
                          height=ecfg["height"], crf=ecfg["crf"], preset=ecfg["preset"],
                          max_duration=ecfg["max_duration_s"])

            stream = db.one("SELECT url FROM streams WHERE platform=? AND channel=?", (platform, channel)) or {}
            meta = generate_metadata(self.cfg, self.budget, channel, platform, transcript, signals,
                                     source_url=u if (u := stream.get("url") or "").startswith("http") else "", seed=clip_id)
            db.update("clips", clip_id, vertical_path=str(vertical), title=meta.title, description=meta.description,
                      tags=json.dumps(meta.tags), status="ready")
            steps["metadata"] = meta.source
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

    def __post_init__(self):
        c, d = self.cfg["capture"], self.cfg["detection"]
        self.buffer = RollingBuffer(key=self.spec.key, out_dir=self.cfg.data_dir / "buffers" / f"{safe_name(self.spec.key)}_{int(time.time())}",
                                    source=self.spec.source, quality=c["quality"], segment_s=c["segment_s"],
                                    buffer_s=c["buffer_s"], simulate_file=self.spec.platform == "file")
        self.chat_window = ChatWindow(d["hype_terms"])
        self.detector = MomentDetector(weights=d["weights"], threshold=d["threshold"], cooldown_s=d["cooldown_s"])
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
                analysis = await loop.run_in_executor(self.analysis_pool, analyze_segment, seg.path)
                feats = self.detector.features(seg.duration, count, hype, analysis)
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
            if not self.buffer.segments:
                status, err = "error", self.buffer.tail_log()
        except asyncio.CancelledError:
            status, err = "stopped", None
        finally:
            self.stop.set()
            self.buffer.stop()
            for t in self.tasks:
                t.cancel()
        self.buffer.cleanup()  # pending clips were extracted above  # clips are already extracted; free the rolling buffer
        self.db.upsert_stream(self.spec.platform, self.spec.channel, status=status, error=err)
        self.db.log("info" if status != "error" else "error", key, f"capture {status}" + (f": {err[-300:]}" if err else ""))

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
        if self.cfg["publish"]["mode"] == "twitch_clip" and self.spec.platform == "twitch":
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
        return select(found, self.cfg, dcfg["max_streams"])

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
            if now < state["next"] or len(self.workers) >= self.cfg["discovery"]["max_streams"]:
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

    async def run_forever(self) -> None:
        dcfg, scfg = self.cfg["discovery"], self.cfg["storage"]
        if secret("TWITCH_CLIENT_ID") and secret("TWITCH_CLIENT_SECRET") and self.cfg.get("twitch_watch", {}).get("enabled", True):
            self._watch_task = asyncio.create_task(self.watch_twitch_forever())
        else:
            self.db.log("warning", "twitch_watch", "disabled: TWITCH_CLIENT_ID/SECRET not set; no auto-reconnect")
        last_cleanup = 0.0
        while True:
            for key, (w, t) in list(self.workers.items()):
                if t.done() and not (key.startswith("twitch:") and hasattr(self, "_watch_task")):
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
                if key in self.workers or len(self.workers) >= dcfg["max_streams"]:
                    continue
                spec = StreamSpec(s.platform, s.channel, s.url, channel_id=s.channel_id, live_chat_id=s.live_chat_id)
                try:
                    self.start_stream(spec)
                except PermissionError as e:
                    self.db.log("warning", "orchestrator", str(e))
            await asyncio.sleep(dcfg["interval_s"])
