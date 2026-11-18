"""Diagnose, repair and verify the local Faster-Whisper model.

Models live in the Hugging Face cache under HF_HOME (set to <data_dir>/hf
by the Windows setup):
  <HF_HOME>/hub/models--Systran--faster-whisper-<size>/
      blobs/<sha>[.incomplete]   actual file content (".incomplete" = interrupted download)
      snapshots/<rev>/<file>     the files the model loads (links or copies of blobs)
      refs/main                  revision currently used

`status` works offline. With network it also compares every file's size
and SHA-256 against the Hub. `repair` resumes interrupted downloads, re-fetches
missing or corrupt files, and with --clean deletes the model and downloads it
again from scratch.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from pathlib import Path

REQUIRED = ["config.json", "model.bin", "tokenizer.json"]
VOCAB = ["vocabulary.txt", "vocabulary.json"]
ALLOW = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]


def repo_id(model: str) -> str:
    return model if "/" in model else f"Systran/faster-whisper-{model}"


def hf_home() -> Path:
    return Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface")


def cache_dir() -> Path:
    return Path(os.environ.get("HF_HUB_CACHE") or hf_home() / "hub")


def repo_cache(model: str) -> Path:
    return cache_dir() / ("models--" + repo_id(model).replace("/", "--"))


def snapshot_dir(model: str) -> Path | None:
    rc = repo_cache(model)
    ref = rc / "refs" / "main"
    if ref.exists():
        d = rc / "snapshots" / ref.read_text(encoding="utf-8").strip()
        if d.exists():
            return d
    snaps = sorted((rc / "snapshots").glob("*"), key=lambda p: p.stat().st_mtime, reverse=True) if (rc / "snapshots").exists() else []
    return snaps[0] if snaps else None


def _sha256(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def remote_files(model: str) -> dict[str, dict] | None:
    """{filename: {"size": int, "sha256": str|None}} from the Hub, or None if offline."""
    try:
        from huggingface_hub import HfApi
        info = HfApi().model_info(repo_id(model), files_metadata=True)
    except Exception:
        return None
    out = {}
    for s in info.siblings or []:
        name = s.rfilename
        if not any(re.fullmatch(p.replace(".", r"\.").replace("*", ".*"), name) for p in ALLOW):
            continue
        lfs = getattr(s, "lfs", None)
        sha = (lfs.get("sha256") if isinstance(lfs, dict) else getattr(lfs, "sha256", None)) if lfs else None
        out[name] = {"size": s.size, "sha256": sha}
    return out


def status(model: str, check_remote: bool = True, deep: bool = False) -> dict:
    """Problems are listed in result["problems"]; empty list = model complete."""
    rc, snap = repo_cache(model), snapshot_dir(model)
    res: dict = {"model": model, "repo": repo_id(model), "cache": str(rc), "snapshot": str(snap) if snap else None,
                 "files": {}, "incomplete_downloads": [], "problems": [], "remote_checked": False}
    blobs = rc / "blobs"
    if blobs.exists():
        for b in blobs.glob("*.incomplete"):
            res["incomplete_downloads"].append({"file": b.name, "bytes": b.stat().st_size})
    if res["incomplete_downloads"]:
        res["problems"].append(f"{len(res['incomplete_downloads'])} interrupted download(s) in {blobs}")
    if not snap:
        res["problems"].append("model not downloaded")
        return res
    for name in REQUIRED + VOCAB + ["preprocessor_config.json"]:
        p = snap / name
        if p.exists():
            try:
                res["files"][name] = {"bytes": p.stat().st_size}
            except OSError:  # dangling symlink
                res["problems"].append(f"{name}: broken link")
    for name in REQUIRED:
        if name not in res["files"]:
            res["problems"].append(f"{name}: missing")
    if not any(v in res["files"] for v in VOCAB):
        res["problems"].append("vocabulary file: missing")
    remote = remote_files(model) if check_remote else None
    if remote:
        res["remote_checked"] = True
        for name, meta in remote.items():
            local = res["files"].get(name)
            if not local:
                if name in REQUIRED or name in VOCAB:
                    res["problems"].append(f"{name}: missing (Hub has {meta['size']} bytes)") if f"{name}: missing" not in res["problems"] else None
                continue
            local["expected_bytes"] = meta["size"]
            if meta["size"] is not None and local["bytes"] != meta["size"]:
                res["problems"].append(f"{name}: size {local['bytes']} != expected {meta['size']} (truncated/corrupt)")
            elif deep and meta.get("sha256"):
                ok = _sha256(snap / name) == meta["sha256"]
                local["sha256_ok"] = ok
                if not ok:
                    res["problems"].append(f"{name}: SHA-256 mismatch (corrupt)")
    return res


def _download(model: str, attempts: int = 4) -> Path:
    from huggingface_hub import snapshot_download
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")       # slow home connections
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")  # Windows without Developer Mode copies files
    last = None
    for i in range(attempts):
        try:
            # resumes any *.incomplete blob instead of starting over
            return Path(snapshot_download(repo_id(model), allow_patterns=ALLOW, cache_dir=str(cache_dir())))
        except Exception as e:
            last = e
            time.sleep(min(5 * 2 ** i, 60))
    raise RuntimeError(f"download failed after {attempts} attempts: {last}")


def repair(model: str, clean: bool = False, log=print) -> dict:
    rc = repo_cache(model)
    if clean and rc.exists():
        log(f"deleting {rc} for a clean download")
        shutil.rmtree(rc)
    before = status(model)
    if not before["problems"] and not clean:
        log("model already complete; nothing to repair")
        return before
    for p in before["problems"]:
        log(f"found: {p}")
    snap = snapshot_dir(model)
    # files with the wrong size will not be fixed by a resume: remove them first
    for p in before["problems"]:
        m = re.match(r"([\w.\-]+): (size|SHA-256)", p)
        if m and snap and (snap / m.group(1)).exists():
            target = (snap / m.group(1)).resolve()
            log(f"removing corrupt {m.group(1)}")
            (snap / m.group(1)).unlink()
            if target.exists() and target.parent.name == "blobs":
                target.unlink()
    log(f"downloading/resuming {repo_id(model)} into {cache_dir()} ...")
    _download(model)
    after = status(model, deep=True)
    if after["problems"] and not clean:
        log("still incomplete after resume -> clean re-download")
        return repair(model, clean=True, log=log)
    return after


# ---- verification -----------------------------------------------------------
def _norm_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def word_recall(expected: str, got: str) -> float:
    """Share of expected words found in the transcript, in order-insensitive
    multiset terms (robust to punctuation and small insertions)."""
    exp, have = _norm_words(expected), _norm_words(got)
    if not exp:
        return 1.0
    pool: dict[str, int] = {}
    for w in have:
        pool[w] = pool.get(w, 0) + 1
    hit = 0
    for w in exp:
        if pool.get(w, 0) > 0:
            pool[w] -= 1
            hit += 1
    return hit / len(exp)


def verify(model: str, media: str | Path, expected: str | None = None, threads: int = 2,
           compute_type: str = "int8", min_recall: float = 0.6) -> dict:
    from .ffmpeg_utils import duration
    from .transcribe import Transcriber
    t = Transcriber(model, compute_type, None, cpu_threads=threads)
    t0 = time.time()
    t._load()
    load_s = time.time() - t0
    t0 = time.time()
    words, lang = t.words(media)
    proc_s = time.time() - t0
    text = " ".join(w.text for w in words)
    dur = duration(media)
    res = {"model": model, "media": str(media), "language": lang, "words": len(words), "text": text,
           "load_s": round(load_s, 1), "audio_s": round(dur, 1), "processing_s": round(proc_s, 1),
           "rtf": round(proc_s / max(dur, 0.1), 2), "ok": len(words) > 0}
    if expected is not None:
        res["recall"] = round(word_recall(expected, text), 2)
        res["ok"] = res["ok"] and res["recall"] >= min_recall
    return res
