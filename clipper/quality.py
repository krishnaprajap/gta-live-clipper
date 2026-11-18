"""Quality gate and duplicate detection for clip candidates."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .ffmpeg_utils import black_ratio, duration, frame_hashes, has_audio, mean_volume


@dataclass
class QualityResult:
    ok: bool
    reason: str
    duration: float
    metrics: dict


def check_quality(path: Path, min_duration: float = 8.0, max_black: float = 0.5, min_volume_db: float = -55.0) -> QualityResult:
    d = duration(path)
    m: dict = {"duration": round(d, 2)}
    if d < min_duration:
        return QualityResult(False, f"too short ({d:.1f}s)", d, m)
    br = black_ratio(path, d)
    m["black_ratio"] = round(br, 3)
    if br > max_black:
        return QualityResult(False, f"mostly black ({br:.0%})", d, m)
    if has_audio(path):
        mv = mean_volume(path)
        m["mean_volume_db"] = mv
        if mv is not None and mv < min_volume_db:
            return QualityResult(False, f"near-silent ({mv} dB)", d, m)
    return QualityResult(True, "ok", d, m)


def fingerprint(path: Path) -> str:
    return ",".join(f"{h:016x}" for h in frame_hashes(path))


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def similarity(fp_a: str, fp_b: str) -> float:
    """Best average frame-hash similarity over small alignment shifts (0..1)."""
    a = [int(x, 16) for x in fp_a.split(",") if x]
    b = [int(x, 16) for x in fp_b.split(",") if x]
    if not a or not b:
        return 0.0
    best = 0.0
    for shift in range(-2, 3):
        pairs = [(a[i], b[i + shift]) for i in range(len(a)) if 0 <= i + shift < len(b)]
        if len(pairs) < 3:
            continue
        sim = 1 - sum(_hamming(x, y) for x, y in pairs) / (64 * len(pairs))
        best = max(best, sim)
    return best


def overlap_ratio(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    shortest = min(a[1] - a[0], b[1] - b[0])
    return inter / shortest if shortest > 0 else 0.0
