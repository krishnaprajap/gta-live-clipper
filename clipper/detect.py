"""Moment detection: per-segment features -> robust z-scores against a rolling
baseline -> weighted score. A moment triggers when the score crosses the
threshold outside the cooldown; consecutive high segments extend the peak."""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field


class RollingStat:
    """Rolling median/MAD baseline (robust to the spikes we want to detect)."""

    def __init__(self, size: int = 45, min_scale: float = 1e-3):
        self.vals: deque[float] = deque(maxlen=size)
        self.min_scale = min_scale

    def z(self, x: float) -> float:
        if len(self.vals) < 5:
            return 0.0
        s = sorted(self.vals)
        med = s[len(s) // 2]
        mad = sorted(abs(v - med) for v in s)[len(s) // 2]
        scale = max(1.4826 * mad, self.min_scale)
        return (x - med) / scale

    def push(self, x: float) -> None:
        self.vals.append(x)


@dataclass
class Detection:
    t: float           # capture time of the peak segment end
    score: float
    signals: dict


@dataclass
class MomentDetector:
    weights: dict
    threshold: float = 3.0
    cooldown_s: float = 60
    warmup_segments: int = 6
    z_cap: float = 6.0
    stats: dict = field(default_factory=dict)
    seen: int = 0
    last_trigger: float = -math.inf
    history: deque = field(default_factory=lambda: deque(maxlen=200))

    def __post_init__(self) -> None:
        # minimum scales stop tiny absolute changes in quiet streams looking huge
        mins = {"chat_rate": 0.3, "chat_hype": 0.15, "audio": 2.0, "scene": 3.0, "speech": 0.5}
        self.stats = {k: RollingStat(min_scale=mins.get(k, 1e-3)) for k in self.weights}

    def features(self, seg_dur: float, chat_count: int, chat_hype: int, analysis: dict) -> dict:
        d = max(seg_dur, 0.5)
        f = {"chat_rate": chat_count / d, "chat_hype": chat_hype / d}
        if analysis.get("rms_db") is not None:
            f["audio"] = max(analysis["rms_db"], -90.0)
        f["scene"] = analysis.get("scene_max", 0.0)
        return f

    def update(self, t: float, feats: dict) -> Detection | None:
        zs, score = {}, 0.0
        for k, w in self.weights.items():
            if k not in feats:
                continue
            z = max(0.0, min(self.stats[k].z(feats[k]), self.z_cap))
            zs[k] = round(z, 2)
            score += w * z
        # normalise so threshold doesn't depend on how many signals are present
        wsum = sum(self.weights[k] for k in zs) or 1.0
        score = score / wsum * 2.0
        for k, v in feats.items():
            if k in self.stats:
                # don't let a spike poison the baseline immediately
                self.stats[k].push(v if zs.get(k, 0) < 3 else self.stats[k].vals[-1] if self.stats[k].vals else v)
        self.seen += 1
        rec = {"t": round(t, 2), "score": round(score, 2), "z": zs, "raw": {k: round(v, 2) for k, v in feats.items()}}
        self.history.append(rec)
        if self.seen <= self.warmup_segments:
            return None
        if score >= self.threshold and t - self.last_trigger >= self.cooldown_s:
            self.last_trigger = t
            return Detection(t=t, score=score, signals=rec)
        return None
