import os
import time

import pytest
from fastapi.testclient import TestClient

from clipper.config import load_config
from clipper.dashboard.app import create_app, token_problem
from clipper.pipeline import Orchestrator
from clipper.storage import cleanup

GOOD = "x" * 32


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", GOOD)
    cfg = load_config(overrides={"data_dir": str(tmp_path), "transcription": {"enabled": False}})
    return TestClient(create_app(cfg, Orchestrator(cfg)))


def test_token_strength():
    assert token_problem(None) and token_problem("change-me") and token_problem("short")
    assert token_problem(GOOD) is None


@pytest.mark.parametrize("path", ["/", "/api/status", "/api/config", "/media/1", "/docs", "/openapi.json"])
def test_everything_requires_auth(client, path):
    r = client.get(path)
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    assert client.get(path, auth=("u", "wrong-wrong-wrong-wrong")).status_code == 401


def test_actions_require_auth(client):
    assert client.post("/api/clips/1/publish").status_code == 401
    assert client.post("/api/streams/x/stop").status_code == 401


def test_valid_credentials(client):
    assert client.get("/", auth=("any", GOOD)).status_code == 200
    assert client.get("/api/status", headers={"X-Token": GOOD}).status_code == 200
    body = client.get("/api/config", auth=("a", GOOD)).text
    assert GOOD not in body  # secrets reported as set/unset only


def test_healthz_is_public_and_minimal(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_fails_closed_when_token_removed(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "change-me")
    assert client.get("/api/status", headers={"X-Token": "change-me"}).status_code == 503


def test_cleanup_removes_old_outbox_files(tmp_path):
    from clipper.db import DB
    out = tmp_path / "outbox"
    out.mkdir()
    old, new = out / "old.mp4", out / "new.mp4"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    os.utime(old, (time.time() - 5 * 86400,) * 2)
    cleanup(DB(tmp_path / "c.db"), tmp_path, max_disk_gb=10, keep_clips_days=3, keep_published_days=1)
    assert not old.exists() and new.exists()
