"""Local transcription with faster-whisper (CPU, int8). Produces short,
Shorts-style caption chunks (a few words each) as SRT."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Word:
    start: float
    end: float
    text: str


class TranscriptionUnavailable(RuntimeError):
    pass


class Transcriber:
    _lock = threading.Lock()

    def __init__(self, model: str = "small", compute_type: str = "int8", language: str | None = None,
                 cpu_threads: int = 2):
        self.model_name = model
        self.compute_type = compute_type
        self.language = language
        self.cpu_threads = cpu_threads
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as e:
                raise TranscriptionUnavailable("faster-whisper not installed") from e
            try:
                self._model = WhisperModel(self.model_name, device="cpu", compute_type=self.compute_type,
                                           cpu_threads=self.cpu_threads)
            except Exception as e:  # model download / load failure
                raise TranscriptionUnavailable(f"could not load whisper model '{self.model_name}': {e}") from e
        return self._model

    def words(self, media: str | Path) -> tuple[list[Word], str]:
        with self._lock:  # one transcription at a time keeps CPU bounded
            model = self._load()
            segments, info = model.transcribe(str(media), language=self.language, word_timestamps=True,
                                              vad_filter=True, beam_size=1)
            words = []
            for seg in segments:
                for w in seg.words or []:
                    words.append(Word(w.start, w.end, w.word.strip()))
        return words, getattr(info, "language", "") or ""


def chunk_words(words: list[Word], max_words: int = 4, max_gap: float = 0.6, max_len_s: float = 2.2) -> list[Word]:
    chunks: list[Word] = []
    cur: list[Word] = []
    for w in words:
        if not w.text:
            continue
        if cur and (len(cur) >= max_words or w.start - cur[-1].end > max_gap or w.end - cur[0].start > max_len_s):
            chunks.append(Word(cur[0].start, cur[-1].end, " ".join(x.text for x in cur)))
            cur = []
        cur.append(w)
    if cur:
        chunks.append(Word(cur[0].start, cur[-1].end, " ".join(x.text for x in cur)))
    return chunks


def _ts(t: float) -> str:
    t = max(t, 0.0)
    h, rem = divmod(int(t * 1000), 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(chunks: list[Word], path: str | Path) -> Path:
    path = Path(path)
    lines = []
    for i, c in enumerate(chunks, 1):
        lines += [str(i), f"{_ts(c.start)} --> {_ts(max(c.end, c.start + 0.3))}", c.text.upper(), ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
