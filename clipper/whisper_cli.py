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
        return _verify_latest_clip(cfg)
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


def _verify_latest_clip(cfg) -> int:
    from .db import DB
    db = DB(cfg.data_dir / "clipper.db")
    c = db.one("SELECT id, status, error, transcript, srt_path FROM clips ORDER BY id DESC LIMIT 1")
    if not c:
        print("no clips yet")
        return 1
    srt_ok = bool(c["srt_path"]) and Path(c["srt_path"]).exists() and Path(c["srt_path"]).stat().st_size > 0
    words = len((c["transcript"] or "").split())
    print(f"clip {c['id']}: status={c['status']} words={words} srt={'yes' if srt_ok else 'no'} error={c['error']}")
    print("transcript:", (c["transcript"] or "(empty)")[:300])
    ok = srt_ok and words > 0 and c["status"] != "caption_failed"
    print("RESULT:", "PASS (captions burned into the clip)" if ok else "FAIL")
    return 0 if ok else 1
