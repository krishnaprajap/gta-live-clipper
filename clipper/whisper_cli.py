"""`python -m clipper whisper status|repair|verify`"""
from __future__ import annotations

import json
from pathlib import Path

from . import whisper_model as wm


def run_whisper(cfg, args) -> int:
    model = args.model or cfg["transcription"]["model"]
    print(f"model: {model}  ({wm.repo_id(model)})\ncache: {wm.cache_dir()}")
    if args.action == "status":
        st = wm.status(model, deep=args.deep)
        _print_status(st)
        return 0 if not st["problems"] else 1
    if args.action == "repair":
        st = wm.repair(model, clean=args.clean)
        _print_status(st)
        if st["problems"]:
            return 1
        print("model files complete; test-loading ...")
        from .transcribe import Transcriber
        Transcriber(model, cfg["transcription"]["compute_type"])._load()
        print("model loads OK. Next: python -m clipper whisper verify --media <file with speech>")
        return 0
    # verify
    if args.latest_clip:
        expected = Path(args.expect_file).read_text(encoding="utf-8-sig") if args.expect_file else None
        return _verify_latest_clip(cfg, expected)
    if not args.media:
        print("verify needs --media <file with speech> (and ideally --expect-file <transcript.txt>) or --latest-clip")
        return 2
    expected = Path(args.expect_file).read_text(encoding="utf-8-sig") if args.expect_file else None
    res = wm.verify(model, args.media, expected, threads=int(cfg["transcription"].get("cpu_threads", 2)),
                    compute_type=cfg["transcription"]["compute_type"])
    print(json.dumps({k: v for k, v in res.items() if k != "text"}, indent=1))
    print("transcript:", res["text"][:500] or "(empty)")
    print("RESULT:", "PASS" if res["ok"] else "FAIL" + (" (no words recognised)" if not res["words"] else " (transcript does not match expected text)"))
    return 0 if res["ok"] else 1


def _print_status(st: dict) -> None:
    print(f"snapshot: {st['snapshot']}")
    for name, f in st["files"].items():
        exp = f.get("expected_bytes")
        print(f"  {name:26} {f['bytes']:>12,} bytes" + (f" (expected {exp:,})" if exp is not None else ""))
    for d in st["incomplete_downloads"]:
        print(f"  interrupted download: {d['file']} ({d['bytes']:,} bytes so far)")
    if not st["remote_checked"]:
        print("  (Hub not reachable: sizes not compared with the server)")
    if st["problems"]:
        print("PROBLEMS:\n  - " + "\n  - ".join(st["problems"]))
        print("Fix: python -m clipper whisper repair    (or: whisper repair --clean)")
    else:
        print("STATUS: complete")


def _first_cue(srt: Path) -> tuple[float, float] | None:
    import re
    m = re.search(r"(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)", srt.read_text(encoding="utf-8"))
    if not m:
        return None
    g = [int(x) for x in m.groups()]
    return g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000, g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000


def _gray_frame(video: Path, t: float, w: int = 270, h: int = 480) -> bytes:
    import subprocess
    from .ffmpeg_utils import FFMPEG
    return subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(video),
                           "-frames:v", "1", "-vf", f"scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
                          capture_output=True, timeout=60).stdout


def captions_burned(raw: Path, vertical: Path, srt: Path, cfg) -> tuple[bool, str]:
    """Render the same frame WITHOUT subtitles and compare the caption area.
    Burned-in text = many strongly different pixels in the lower part of the
    frame, while the upper part (no captions) stays the same."""
    import tempfile
    from .edit import make_vertical
    cue = _first_cue(srt)
    if not cue:
        return False, "SRT has no cues"
    t = (cue[0] + cue[1]) / 2
    e = cfg["edit"]
    with tempfile.TemporaryDirectory() as td:
        ref = Path(td) / "nosubs.mp4"
        make_vertical(raw, ref, srt=None, credit=None,  # credit sits above both compared areas
                      width=e["width"], height=e["height"], crf=e["crf"], preset=e["preset"], max_duration=t + 1.0,
                      threads=e.get("threads", 2))
        a, b = _gray_frame(vertical, t), _gray_frame(ref, t)
    w, h = 270, 480
    if len(a) != w * h or len(b) != w * h:
        return False, "could not read frames"
    def frac(r0, r1):
        n = sum(1 for i in range(r0 * w, r1 * w) if abs(a[i] - b[i]) > 60)
        return n / ((r1 - r0) * w)
    low, top = frac(int(h * 0.6), h), frac(int(h * 0.15), int(h * 0.45))
    ok = low >= 0.003 and low > 5 * top
    return ok, f"at {t:.2f}s: {low:.2%} of caption-area pixels differ from a no-subtitle render (top area {top:.2%})"


def _verify_latest_clip(cfg, expected: str | None = None) -> int:
    from .db import DB
    db = DB(cfg.data_dir / "clipper.db")
    c = db.one("SELECT id, status, error, transcript, srt_path, raw_path, vertical_path FROM clips ORDER BY id DESC LIMIT 1")
    if not c:
        print("no clips yet")
        return 1
    srt_ok = bool(c["srt_path"]) and Path(c["srt_path"]).exists() and Path(c["srt_path"]).stat().st_size > 0
    words = len((c["transcript"] or "").split())
    print(f"clip {c['id']}: status={c['status']} words={words} srt={'yes' if srt_ok else 'no'} error={c['error']}")
    print("transcript:", (c["transcript"] or "(empty)")[:300])
    checks = {"clip processed (not duplicate/failed)": c["status"] not in ("duplicate", "rejected", "failed", "caption_failed"),
              "transcript has words": words > 0, "SRT file written": srt_ok}
    if expected is not None:
        r = wm.word_recall(expected, c["transcript"] or "")
        print(f"recall vs expected sentence: {r:.2f}")
        checks["transcript matches spoken sentence (recall >= 0.6)"] = r >= 0.6
    if srt_ok and c["vertical_path"] and Path(c["vertical_path"]).exists():
        ok, detail = captions_burned(Path(c["raw_path"]), Path(c["vertical_path"]), Path(c["srt_path"]), cfg)
        print("burn-in:", detail)
        checks["captions burned into vertical video"] = ok
    else:
        checks["captions burned into vertical video"] = False
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    ok = all(checks.values())
    print("RESULT:", "PASS (captions burned into the clip)" if ok else "FAIL")
    if ok:
        print("video:", c["vertical_path"])
    return 0 if ok else 1
