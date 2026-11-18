"""Lightweight dashboard: one FastAPI app, one HTML page, JSON polling.
Read endpoints are open on the bound interface; actions require
DASHBOARD_TOKEN (X-Token header). Bind to 127.0.0.1 or put it behind a
reverse proxy with TLS when deployed."""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from ..config import Config, secret
from ..pipeline import Orchestrator, StreamSpec

HTML = (Path(__file__).parent / "index.html").read_text()


def create_app(cfg: Config, orch: Orchestrator, loop: asyncio.AbstractEventLoop | None = None) -> FastAPI:
    app = FastAPI(title="GTA VI Clipper")
    db = orch.db

    def auth(token: str | None) -> None:
        expected = secret("DASHBOARD_TOKEN")
        if not expected or expected == "change-me" or token != expected:
            raise HTTPException(401, "invalid or unset DASHBOARD_TOKEN")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTML

    @app.get("/api/status")
    def status():
        clips = db.query("SELECT id, stream_key, status, title, duration, error, created_at, vertical_path FROM clips "
                         "ORDER BY id DESC LIMIT 50")
        for c in clips:
            c["has_video"] = bool(c.pop("vertical_path"))
        return {
            "streams": db.query("SELECT platform, channel, title, viewers, status, error, updated_at FROM streams "
                                "ORDER BY updated_at DESC LIMIT 50"),
            "active": [{"key": k, "segments": len(w.buffer.segments), "chat_msgs": w.chat_window.total,
                        "chat_connected": bool(w.chat and w.chat.connected), "chat_error": w.chat and w.chat.error,
                        "last_scores": list(w.detector.history)[-30:]} for k, (w, _) in orch.workers.items()],
            "moments": db.query("SELECT id, stream_key, t, score, status FROM moments ORDER BY id DESC LIMIT 30"),
            "clips": clips,
            "jobs": db.query("SELECT id, clip_id, target, status, remote_url, error, attempts FROM publish_jobs "
                             "ORDER BY id DESC LIMIT 30"),
            "events": db.query("SELECT level, source, message, created_at FROM events ORDER BY id DESC LIMIT 40"),
            "budget": orch.budget.summary(),
            "publish_mode": cfg["publish"]["mode"],
        }

    @app.get("/api/config")
    def config():
        return cfg.redacted()

    @app.get("/media/{clip_id}")
    def media(clip_id: int):
        c = db.one("SELECT vertical_path FROM clips WHERE id=?", (clip_id,))
        if not c or not c["vertical_path"] or not Path(c["vertical_path"]).exists():
            raise HTTPException(404)
        return FileResponse(c["vertical_path"], media_type="video/mp4")

    @app.post("/api/clips/{clip_id}/publish")
    def republish(clip_id: int, x_token: str | None = Header(None)):
        auth(x_token)
        c = db.one("SELECT * FROM clips WHERE id=?", (clip_id,))
        if not c or not c["vertical_path"]:
            raise HTTPException(404)
        meta = {"title": c["title"], "description": c["description"], "tags": json.loads(c["tags"] or "[]")}
        return orch.processor.publish(clip_id, Path(c["vertical_path"]), meta)

    @app.post("/api/streams/start")
    def start(body: dict, x_token: str | None = Header(None)):
        auth(x_token)
        if loop is None:
            raise HTTPException(503, "orchestrator loop not running")
        spec = StreamSpec(body["platform"], body["channel"], body["source"], channel_id=body.get("channel_id", ""),
                          live_chat_id=body.get("live_chat_id", ""))
        try:
            asyncio.run_coroutine_threadsafe(_start(orch, spec), loop).result(10)
        except PermissionError as e:
            raise HTTPException(403, str(e))
        return {"started": spec.key}

    @app.post("/api/streams/{key}/stop")
    def stop(key: str, x_token: str | None = Header(None)):
        auth(x_token)
        w = orch.workers.get(key)
        if not w:
            raise HTTPException(404)
        loop.call_soon_threadsafe(w[0].stop.set)
        return {"stopping": key}

    return app


async def _start(orch: Orchestrator, spec: StreamSpec):
    orch.start_stream(spec)


def serve(cfg: Config) -> None:
    import uvicorn
    loop = asyncio.new_event_loop()
    orch_holder: dict = {}

    def run_loop():
        asyncio.set_event_loop(loop)
        orch_holder["orch"] = Orchestrator(cfg)
        ready.set()
        loop.run_until_complete(orch_holder["orch"].run_forever())

    ready = threading.Event()
    threading.Thread(target=run_loop, daemon=True, name="orchestrator").start()
    ready.wait()
    app = create_app(cfg, orch_holder["orch"], loop)
    d = cfg["dashboard"]
    uvicorn.run(app, host=d["host"], port=d["port"], log_level="warning")
