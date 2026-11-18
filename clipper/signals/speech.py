"""Optional live speech signal: transcribe each buffer segment with a small
Whisper model and score excitement (hype words, shouting-style short bursts).
Off by default because it costs CPU on every segment of every stream."""
from __future__ import annotations

import re
import threading

from ..transcribe import Transcriber


class _OwnLock(Transcriber):
    _lock = threading.Lock()  # separate from clip transcription, so neither blocks the other


class SpeechSignal:
    def __init__(self, hype_terms: list[str], model: str = "tiny", threads: int = 1):
        self.t = _OwnLock(model, "int8", None, cpu_threads=threads)
        words = [w for w in hype_terms if re.match(r"^[\w ]+$", w)]
        self.rx = re.compile(r"\b(" + "|".join(map(re.escape, words)) + r")\b", re.I) if words else None
        self.error: str | None = None

    def score(self, segment_path: str, seg_dur: float) -> float | None:
        """hype words + 0.2 * words per second; None if transcription failed."""
        try:
            words, _ = self.t.words(segment_path)
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            return None
        text = " ".join(w.text for w in words)
        hype = len(self.rx.findall(text)) if self.rx else 0
        return hype + 0.2 * len(words) / max(seg_dur, 0.5)
