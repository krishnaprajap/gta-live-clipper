"""Command line entry point.

  python -m clipper.cli run --file sample.mp4 --chat chat.jsonl   # offline end-to-end test
  python -m clipper.cli run --twitch <channel>                     # one authorized Twitch stream
  python -m clipper.cli run --youtube <video_url> --channel-id UC.. # one authorized YouTube stream
  python -m clipper.cli discover                                   # list live GTA VI streams
  python -m clipper.cli serve                                      # dashboard + autonomous mode
  python -m clipper.cli cleanup
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from .config import load_config, secret
from .pipeline import Orchestrator, StreamSpec


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="clipper")
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="capture one stream until it ends (or Ctrl+C)")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--file", help="local authorized recording, played back as if live")
    g.add_argument("--twitch", help="Twitch channel login")
    g.add_argument("--youtube", help="YouTube live watch URL")
    r.add_argument("--channel", help="display name (file mode)")
    r.add_argument("--channel-id", default="", help="Twitch broadcaster id / YouTube channel id")
    r.add_argument("--live-chat-id", default="")
    r.add_argument("--chat", help="JSONL chat replay for file mode")
    sub.add_parser("discover")
    sv = sub.add_parser("serve", help="dashboard + autonomous mode")
    sv.add_argument("--twitch", help="also start capturing this authorized Twitch channel immediately")
    sv.add_argument("--channel-id", default="")
    sv.add_argument("--log-file", help="also log to this file (rotating, 3 x 10 MB)")
    sub.add_parser("doctor", help="check environment variables, binaries and config (prints no secret values)")
    wh = sub.add_parser("whisper", help="Whisper model: status | repair [--clean] | verify")
    wh.add_argument("action", choices=["status", "repair", "verify"])
    wh.add_argument("--model", help="default: transcription.model from config")
    wh.add_argument("--clean", action="store_true", help="repair: delete the model and download it from scratch")
    wh.add_argument("--deep", action="store_true", help="status: also verify SHA-256 of every file (slow)")
    wh.add_argument("--media", help="verify: audio/video file with speech")
    wh.add_argument("--expect-file", help="verify: text file with what is said in --media")
    wh.add_argument("--latest-clip", action="store_true", help="verify: check the newest clip has captions")
    tc = sub.add_parser("twitch-check", help="verify Twitch credentials, GTA VI category, channels (read-only)")
    tc.add_argument("--chat", type=int, default=0, help="also read chat of the first channel for N seconds")
    cr = sub.add_parser("credentials", help="enter credentials into .env without showing them")
    cr.add_argument("service", choices=["twitch"])
    sub.add_parser("cleanup")
    args = ap.parse_args(argv)
    handlers: list[logging.Handler] = []
    if not getattr(args, "log_file", None):
        handlers.append(logging.StreamHandler())
    else:  # file only: stderr is kept for crash tracebacks (captured by the Windows supervisor)
        from logging.handlers import RotatingFileHandler
        Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(args.log_file, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    cfg = load_config(args.config)
    if args.cmd in ("run", "serve"):
        from .system import apply_resource_limits, set_app_priority
        res = cfg.get("resources", {}) or {}
        log = logging.getLogger("clipper")
        log.info("process priority: %s", set_app_priority(res.get("process_priority", "below_normal")))
        log.info("resource limits: %s", apply_resource_limits(res))
        log.info("max simultaneous streams: %s", cfg.max_streams)

    if args.cmd == "run":
        if args.file:
            spec = StreamSpec("file", args.channel or "local_test", args.file, chat_replay=args.chat or "")
        elif args.twitch:
            spec = StreamSpec("twitch", args.twitch.lower(), f"https://www.twitch.tv/{args.twitch}", channel_id=args.channel_id)
        else:
            spec = StreamSpec("youtube", args.channel_id or args.youtube, args.youtube, channel_id=args.channel_id,
                              live_chat_id=args.live_chat_id)

        async def go():
            orch = Orchestrator(cfg)
            try:
                await orch.run_single(spec)
            finally:
                rows = orch.db.query("SELECT c.id, c.status, c.title, c.vertical_path, c.error, p.status AS pub, "
                                     "p.remote_url FROM clips c LEFT JOIN publish_jobs p ON p.clip_id=c.id")
                print(json.dumps(rows, indent=2))
        try:
            asyncio.run(go())
        except KeyboardInterrupt:
            pass
    elif args.cmd == "discover":
        orch = Orchestrator(cfg)
        chosen, ranked = orch._discover()
        if not (secret("TWITCH_CLIENT_ID") or secret("YOUTUBE_API_KEY")):
            print("No TWITCH_CLIENT_ID/SECRET or YOUTUBE_API_KEY set; nothing to discover.")
        for s in ranked:
            print(f"{'AUTH' if s.authorized else '----'} {s.platform:8} {s.viewers:>7} {s.channel:25} {s.title[:60]}")
        print(f"selected for capture: {[s.channel for s in chosen]}")
    elif args.cmd == "serve":
        from .dashboard.app import serve
        initial = [StreamSpec("twitch", args.twitch.lower(), f"https://www.twitch.tv/{args.twitch}",
                              channel_id=args.channel_id)] if args.twitch else []
        serve(cfg, initial)
    elif args.cmd == "whisper":
        from .whisper_cli import run_whisper
        raise SystemExit(run_whisper(cfg, args))
    elif args.cmd == "twitch-check":
        from .twitch_setup import twitch_check
        raise SystemExit(twitch_check(cfg, chat_seconds=args.chat))
    elif args.cmd == "credentials":
        from .twitch_setup import prompt_twitch_credentials
        raise SystemExit(prompt_twitch_credentials())
    elif args.cmd == "doctor":
        from .doctor import run_doctor
        raise SystemExit(run_doctor(cfg))
    elif args.cmd == "cleanup":
        from .storage import cleanup
        s = cfg["storage"]
        print(cleanup(Orchestrator(cfg).db, cfg.data_dir, s["max_disk_gb"], s["keep_clips_days"], s["keep_published_days"]))


if __name__ == "__main__":
    main()
