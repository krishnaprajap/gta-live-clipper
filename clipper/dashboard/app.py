"""Lightweight dashboard: one FastAPI app, one HTML page, JSON polling.

Every route except /healthz requires authentication: HTTP Basic auth
(any username, password = DASHBOARD_TOKEN) or an `X-Token` header. The
server refuses to start without a strong DASHBOARD_TOKEN. It binds to
127.0.0.1 by default; reach it through an SSH tunnel or a TLS reverse proxy."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import os
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from ..config import Config, secret
from ..pipeline import Orchestrator, StreamSpec

HTML = (Path(__file__).parent / "index.html").read_text()
MIN_TOKEN_LEN = 20
WEAK_TOKENS = {"change-me", "changeme", "password", "admin", "secret"}


def token_problem(token: str | None) -> str | None:
    if not token:
        return "DASHBOARD_TOKEN is not set"
    if token.lower() in WEAK_TOKENS or len(token) < MIN_TOKEN_LEN:
        return f"DASHBOARD_TOKEN is too weak (use >= {MIN_TOKEN_LEN} random characters, e.g. `openssl rand -hex 24`)"
    return None


def _presented_token(request: Request) -> str | None:
    if tok := request.headers.get("x-token"):
        return tok
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            _, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
            return pw
        except (binascii.Error, UnicodeDecodeError):
            return None
    return None


def create_app(cfg: Config, orch: Orchestrator, loop: asyncio.AbstractEventLoop | None = None) -> FastAPI:
    app = FastAPI(title="GTA VI Clipper", docs_url=None, redoc_url=None, openapi_url=None)
    db = orch.db

    @app.middleware("http")
    async def require_auth(request: Request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        expected = secret("DASHBOARD_TOKEN")
        if token_problem(expected):  # fail closed if misconfigured at runtime
            return JSONResponse({"detail": "dashboard disabled: " + token_problem(expected)}, status_code=503)
        given = _presented_token(request)
        if not given or not hmac.compare_digest(given.encode(), expected.encode()):
            await asyncio.sleep(1.0)  # slow down guessing
            return Response("authentication required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="clipper", charset="UTF-8"'})
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

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
    def republish(clip_id: int):
        c = db.one("SELECT * FROM clips WHERE id=?", (clip_id,))
        if not c or not c["vertical_path"]:
            raise HTTPException(404)
        meta = {"title": c["title"], "description": c["description"], "tags": json.loads(c["tags"] or "[]")}
        return orch.processor.publish(clip_id, Path(c["vertical_path"]), meta)

    @app.post("/api/streams/start")
    def start(body: dict):
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
    def stop(key: str):
        w = orch.workers.get(key)
        if not w:
            raise HTTPException(404)
        loop.call_soon_threadsafe(w[0].stop.set)
        return {"stopping": key}

    return app


async def _start(orch: Orchestrator, spec: StreamSpec):
    orch.start_stream(spec)


def serve(cfg: Config, initial: list[StreamSpec] | None = None) -> None:
    import uvicorn
    if problem := token_problem(secret("DASHBOARD_TOKEN")):
        raise SystemExit(f"refusing to start dashboard: {problem}")
    loop = asyncio.new_event_loop()
    orch_holder: dict = {}

    def run_loop():
        asyncio.set_event_loop(loop)
        orch = orch_holder["orch"] = Orchestrator(cfg)
        ready.set()

        async def main():
            for spec in initial or []:
                orch.start_stream(spec)
            await orch.run_forever()
        loop.run_until_complete(main())

    ready = threading.Event()
    threading.Thread(target=run_loop, daemon=True, name="orchestrator").start()
    ready.wait()
    app = create_app(cfg, orch_holder["orch"], loop)
    d = cfg["dashboard"]
    host = os.environ.get("DASHBOARD_HOST", d["host"])
    uvicorn.run(app, host=host, port=int(os.environ.get("DASHBOARD_PORT", d["port"])), log_level="warning")
