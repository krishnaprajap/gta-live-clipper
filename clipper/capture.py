"""Rolling on-disk buffer of a live stream.

streamlink (authorized channels only) pipes the stream into ffmpeg, which
stream-copies it into short MPEG-TS segments and appends each finished
segment to a CSV list (filename,start,end in capture-relative seconds).
Old segments are pruned so the buffer never exceeds `buffer_s` on disk,
except segments pinned by a pending clip extraction.
"""
from __future__ import annotations

import asyncio
import csv
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .ffmpeg_utils import FFMPEG, run
from .system import stop_process, streamlink_available, streamlink_cmd


@dataclass
class Segment:
    index: int
    path: Path
    start: float
    end: float
    wall_end: float  # wall-clock time we observed this segment complete

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class RollingBuffer:
    key: str
    out_dir: Path
    source: str                 # stream URL (streamlink) or local file (simulated live)
    quality: str = "best"
    segment_s: int = 4
    buffer_s: int = 240
    simulate_file: bool = False
    stream_timeout_s: float = 60.0
    segments: list[Segment] = field(default_factory=list)
    pins: dict[int, tuple[float, float]] = field(default_factory=dict)
    _procs: list[subprocess.Popen] = field(default_factory=list)
    _read_rows: int = 0
    _pin_seq: int = 0
    stalled: bool = False

    def __post_init__(self) -> None:
        self.out_dir = Path(self.out_dir)
        if self.out_dir.exists():
            shutil.rmtree(self.out_dir)
        self.out_dir.mkdir(parents=True)
        self.list_path = self.out_dir / "segments.csv"
        self.log_path = self.out_dir / "capture.log"

    # -- process management ------------------------------------------------
    def _ffmpeg_segment_args(self, input_args: list[str]) -> list[str]:
        return [FFMPEG, "-hide_banner", "-loglevel", "warning", *input_args,
                "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
                "-f", "segment", "-segment_time", str(self.segment_s), "-reset_timestamps", "1",
                "-segment_list", str(self.list_path), "-segment_list_type", "csv",
                "-segment_list_flags", "+live", str(self.out_dir / "seg_%06d.ts")]

    def start(self) -> None:
        log = open(self.log_path, "ab")
        if self.simulate_file:
            # Plays a local (authorized) recording at real-time speed, exactly
            # like a live input, to test the pipeline offline.
            ff = subprocess.Popen(self._ffmpeg_segment_args(["-re", "-i", self.source]),
                                  stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            self._procs = [ff]
            return
        if not streamlink_available():
            raise RuntimeError("streamlink is not installed")
        sl = subprocess.Popen([*streamlink_cmd(), "--stdout", "--loglevel", "warning",
                               "--hls-live-edge", "2", "--stream-timeout", str(int(self.stream_timeout_s)),
                               self.source, self.quality], stdout=subprocess.PIPE, stderr=log)
        ff = subprocess.Popen(self._ffmpeg_segment_args(["-i", "pipe:0"]), stdin=sl.stdout, stdout=log, stderr=log)
        sl.stdout.close()  # ffmpeg owns the pipe now
        self._procs = [sl, ff]

    @property
    def running(self) -> bool:
        return bool(self._procs) and all(p.poll() is None for p in self._procs)

    def stop(self) -> None:
        for p in self._procs:
            stop_process(p)
        for p in self._procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()

    def tail_log(self, n: int = 800) -> str:
        try:
            return self.log_path.read_bytes()[-n:].decode(errors="replace")
        except OSError:
            return ""

    # -- segment tracking --------------------------------------------------
    def poll_new(self) -> list[Segment]:
        """Return segments completed since the last call."""
        if not self.list_path.exists():
            return []
        with open(self.list_path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        new = []
        now = time.time()
        for row in rows[self._read_rows:]:
            if len(row) < 3:
                break
            name, start, end = row[0], float(row[1]), float(row[2])
            # with reset_timestamps each segment's start/end in the list are
            # still absolute capture times, which is what we want.
            seg = Segment(index=len(self.segments), path=self.out_dir / name, start=start, end=end, wall_end=now)
            self.segments.append(seg)
            new.append(seg)
            self._read_rows += 1
        self.prune()
        return new

    @property
    def head(self) -> float:
        return self.segments[-1].end if self.segments else 0.0

    def pin(self, start: float, end: float) -> int:
        self._pin_seq += 1
        self.pins[self._pin_seq] = (start, end)
        return self._pin_seq

    def unpin(self, pin_id: int) -> None:
        self.pins.pop(pin_id, None)

    def prune(self) -> None:
        cutoff = self.head - self.buffer_s
        for seg in self.segments:
            if seg.end >= cutoff or not seg.path.exists():
                continue
            if any(seg.end > s and seg.start < e for s, e in self.pins.values()):
                continue
            try:
                os.remove(seg.path)
            except OSError:
                pass

    def window(self, start: float, end: float) -> list[Segment]:
        return [s for s in self.segments if s.end > start and s.start < end and s.path.exists()]

    def extract(self, start: float, end: float, out_path: Path) -> Path:
        """Cut [start,end] (capture time) from the buffer into an MP4 with
        frame-accurate re-encoding (cuts at arbitrary times, not keyframes)."""
        segs = self.window(start, end)
        if not segs:
            raise RuntimeError(f"no buffered segments cover {start:.1f}-{end:.1f}")
        out_path = Path(out_path)
        lst = out_path.with_suffix(".concat.txt")
        lst.write_text("".join(f"file '{s.path.resolve().as_posix()}'\n" for s in segs), encoding="utf-8")
        offset = max(0.0, start - segs[0].start)
        dur = min(end, segs[-1].end) - max(start, segs[0].start)
        run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
             "-ss", f"{offset:.3f}", "-t", f"{dur:.3f}", "-map", "0:v:0", "-map", "0:a:0?",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
             "-movflags", "+faststart", str(out_path)], timeout=300)
        lst.unlink(missing_ok=True)
        return out_path

    def cleanup(self) -> None:
        shutil.rmtree(self.out_dir, ignore_errors=True)


async def wait_for_segments(buf: RollingBuffer, poll_s: float = 0.5, stall_s: float | None = None):
    """Async generator yielding new segments until capture stops.
    If capture is still 'running' but produces no segment for `stall_s`
    seconds (hung connection), it is killed so the caller can reconnect."""
    idle_after_stop = 0
    last_new = time.monotonic()
    buf.stalled = False
    while True:
        new = buf.poll_new()
        for s in new:
            yield s
        if new:
            last_new = time.monotonic()
        elif stall_s and buf.running and time.monotonic() - last_new > stall_s:
            buf.stalled = True
            buf.stop()
        if not buf.running:
            idle_after_stop += 1
            if idle_after_stop > 3 and not new:
                return
        await asyncio.sleep(poll_s)
