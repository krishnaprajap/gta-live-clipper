"""Measure faster-whisper on THIS machine (run on the VPS; downloads the
model from huggingface.co on first use, ~75 MB tiny / ~145 MB base / ~480 MB small).

    python scripts/benchmark_whisper.py [audio_or_video_file] [--model small]

Without a file it uses the newest clip in data/clips/. Reports load time,
real-time factor (RTF = processing time / audio length; < 1 is faster than
real time) and peak memory of this process."""
import argparse
import glob
import os
import resource
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from clipper.ffmpeg_utils import duration  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("media", nargs="?")
ap.add_argument("--model", default="small")
ap.add_argument("--threads", type=int, default=2)
ap.add_argument("--no-vad", action="store_true", help="transcribe all audio (worst case; use when the file has little speech)")
a = ap.parse_args()
media = a.media or max((p for p in glob.glob("data/clips/*.mp4") if "vertical" not in p), key=os.path.getmtime, default=None)
if not media:
    sys.exit("no media given and no clips in data/clips/ (run the offline test first, or pass a file with speech)")

from faster_whisper import WhisperModel  # noqa: E402

t = time.time()
model = WhisperModel(a.model, device="cpu", compute_type="int8", cpu_threads=a.threads)
load = time.time() - t
dur = duration(media)
t = time.time()
segs, info = model.transcribe(media, word_timestamps=True, vad_filter=not a.no_vad, beam_size=1)
words = sum(len(s.words or []) for s in segs)
proc = time.time() - t
peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
print(f"model={a.model} threads={a.threads} load={load:.1f}s audio={dur:.1f}s processing={proc:.1f}s "
      f"RTF={proc / max(dur, 0.1):.2f} words={words} language={info.language} peak_rss={peak_mb:.0f}MB")
