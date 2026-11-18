"""Thin wrappers around ffmpeg/ffprobe subprocess calls."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from .system import background_popen_kwargs, find_binary

FFMPEG = find_binary("ffmpeg", "FFMPEG_BINARY")
FFPROBE = find_binary("ffprobe", "FFPROBE_BINARY")


class FFmpegError(RuntimeError):
    pass


def run(args: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
    p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, **background_popen_kwargs())
    if p.returncode != 0:
        raise FFmpegError(f"{' '.join(args[:6])}... failed: {p.stderr[-1500:]}")
    return p


def probe(path: str | Path) -> dict:
    p = run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], 60)
    return json.loads(p.stdout)


def duration(path: str | Path) -> float:
    try:
        return float(probe(path)["format"]["duration"])
    except (KeyError, ValueError, FFmpegError):
        return 0.0


def has_audio(path: str | Path) -> bool:
    try:
        return any(s.get("codec_type") == "audio" for s in probe(path).get("streams", []))
    except FFmpegError:
        return False


_RMS = re.compile(r"lavfi\.astats\.Overall\.RMS_level=(-?[\d.]+|-inf)")
_PEAK = re.compile(r"lavfi\.astats\.Overall\.Peak_level=(-?[\d.]+|-inf)")
_SCENE = re.compile(r"lavfi\.scd\.score=([\d.]+)")
_BLACK = re.compile(r"black_duration:([\d.]+)")


def analyze_segment(path: str | Path, timeout: float = 30) -> dict:
    """One ffmpeg pass over a buffer segment: audio RMS/peak (dB) and scene-change
    scores on a downscaled video. Cheap enough to run on every 4 s segment."""
    path = str(path)
    audio = has_audio(path)
    filters = ["[0:v]fps=4,scale=160:-2,scdet=threshold=8,metadata=print:file=-[vout]"]
    maps = ["-map", "[vout]"]
    if audio:
        filters.append("[0:a]astats=metadata=1:reset=0,ametadata=print:file=-[aout]")
        maps += ["-map", "[aout]"]
    args = [FFMPEG, "-hide_banner", "-nostats", "-i", path, "-filter_complex", ";".join(filters), *maps, "-f", "null", "-"]
    p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    out = p.stdout + p.stderr

    def last(rx: re.Pattern) -> float | None:
        vals = rx.findall(out)
        if not vals:
            return None
        v = vals[-1]
        return -120.0 if v == "-inf" else float(v)

    scenes = [float(s) for s in _SCENE.findall(out)]
    return {
        "rms_db": last(_RMS) if audio else None,
        "peak_db": last(_PEAK) if audio else None,
        "scene_max": max(scenes) if scenes else 0.0,
        "scene_count": sum(1 for s in scenes if s >= 8),
    }


def black_ratio(path: str | Path, dur: float) -> float:
    p = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path), "-vf", "blackdetect=d=0.5:pix_th=0.10",
                        "-an", "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    total = sum(float(x) for x in _BLACK.findall(p.stderr))
    return total / dur if dur > 0 else 1.0


def mean_volume(path: str | Path) -> float | None:
    p = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path), "-af", "volumedetect", "-vn", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", p.stderr)
    return float(m.group(1)) if m else None


def frame_hashes(path: str | Path, count: int = 8) -> list[int]:
    """Difference hashes (64-bit dHash) of `count` evenly spaced frames, computed
    from raw 9x8 grayscale frames — no image libraries needed."""
    dur = duration(path)
    if dur <= 0:
        return []
    fps = max(count / dur, 0.01)
    p = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path), "-vf",
                        f"fps={fps:.5f},scale=9:8:flags=area,format=gray", "-f", "rawvideo", "-"],
                       capture_output=True, timeout=120)
    data = p.stdout
    hashes = []
    for i in range(0, len(data) - 71, 72):
        px = data[i:i + 72]
        h = 0
        for row in range(8):
            for col in range(8):
                h = (h << 1) | (1 if px[row * 9 + col] > px[row * 9 + col + 1] else 0)
        hashes.append(h)
    return hashes[:count]
