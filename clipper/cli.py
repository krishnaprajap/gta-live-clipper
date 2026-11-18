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
    sub.add_parser("serve")
    sub.add_parser("cleanup")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)

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
        serve(cfg)
    elif args.cmd == "cleanup":
        from .storage import cleanup
        s = cfg["storage"]
        print(cleanup(Orchestrator(cfg).db, cfg.data_dir, s["max_disk_gb"], s["keep_clips_days"], s["keep_published_days"]))


if __name__ == "__main__":
    main()
