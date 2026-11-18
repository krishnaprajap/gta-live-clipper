import json
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from clipper import whisper_model as wm
from clipper.config import load_config


# ---------------------------------------------------------------- Whisper cache
def fake_cache(root: Path, files: dict[str, bytes], incomplete: dict[str, bytes] | None = None):
    rc = root / "hub" / "models--Systran--faster-whisper-small"
    snap = rc / "snapshots" / "abc123"
    (rc / "refs").mkdir(parents=True)
    (rc / "refs" / "main").write_text("abc123")
    (rc / "blobs").mkdir(parents=True)
    snap.mkdir(parents=True)
    for n, b in files.items():
        (snap / n).write_bytes(b)
    for n, b in (incomplete or {}).items():
        (rc / "blobs" / f"{n}.incomplete").write_bytes(b)
    return rc, snap


GOOD = {"config.json": b"{}", "model.bin": b"M" * 100, "tokenizer.json": b"{}", "vocabulary.txt": b"a\nb"}
REMOTE = {"config.json": {"size": 2, "sha256": None}, "model.bin": {"size": 100, "sha256": None},
          "tokenizer.json": {"size": 2, "sha256": None}, "vocabulary.txt": {"size": 3, "sha256": None}}


@pytest.fixture
def hf(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    return tmp_path


def test_status_detects_interrupted_and_missing(hf, monkeypatch):
    monkeypatch.setattr(wm, "remote_files", lambda m: None)
    fake_cache(hf, {k: v for k, v in GOOD.items() if k != "model.bin"}, incomplete={"deadbeef": b"x" * 40})
    st = wm.status("small")
    assert any("interrupted" in p for p in st["problems"])
    assert "model.bin: missing" in st["problems"]


def test_status_detects_truncated_file_against_hub(hf, monkeypatch):
    monkeypatch.setattr(wm, "remote_files", lambda m: REMOTE)
    fake_cache(hf, {**GOOD, "model.bin": b"M" * 60})
    st = wm.status("small")
    assert any("model.bin: size 60 != expected 100" in p for p in st["problems"])


def test_status_complete(hf, monkeypatch):
    monkeypatch.setattr(wm, "remote_files", lambda m: REMOTE)
    fake_cache(hf, GOOD)
    assert wm.status("small")["problems"] == []


def test_repair_resumes_then_falls_back_to_clean(hf, monkeypatch):
    monkeypatch.setattr(wm, "remote_files", lambda m: REMOTE)
    rc, snap = fake_cache(hf, {**GOOD, "model.bin": b"M" * 60}, incomplete={"x": b"1"})
    calls = []

    def fake_download(model, attempts=4):
        calls.append(rc.exists() and (rc / "blobs" / "x.incomplete").exists())
        rc2, s2 = (rc, snap) if rc.exists() else fake_cache(hf, {})
        for b in (rc2 / "blobs").glob("*.incomplete"):
            b.unlink()
        for n, data in GOOD.items():
            (s2 / n).write_bytes(data)
        return s2
    monkeypatch.setattr(wm, "_download", fake_download)
    st = wm.repair("small", log=lambda *a: None)
    assert st["problems"] == [] and calls == [True]   # resumed (incomplete kept), corrupt model.bin replaced


def test_repair_clean_deletes_first(hf, monkeypatch):
    monkeypatch.setattr(wm, "remote_files", lambda m: REMOTE)
    rc, _ = fake_cache(hf, GOOD)
    marker = rc / "blobs" / "old"
    marker.write_text("x")

    def fake_download(model, attempts=4):
        assert not marker.exists()
        _, s = fake_cache(hf, GOOD)
        return s
    monkeypatch.setattr(wm, "_download", fake_download)
    assert wm.repair("small", clean=True, log=lambda *a: None)["problems"] == []


def test_word_recall():
    assert wm.word_recall("Watch this car jump", " watch this CAR jump!") == 1.0
    assert wm.word_recall("Watch this car jump", "") == 0.0
    assert wm.word_recall("one two three four", "one two") == 0.5


# ---------------------------------------------------------------- captions never silent
def make_clip(tmp_path):
    cfg = load_config(overrides={"data_dir": str(tmp_path / "d"), "transcription": {"enabled": True},
                                 "publish": {"mode": "dry_run"}})
    from clipper.pipeline import Orchestrator
    orch = Orchestrator(cfg)
    raw = orch.processor.clips_dir / "c.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=10",
                    "-f", "lavfi", "-i", "sine=f=300:duration=10", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", str(raw)], check=True)
    mid = orch.db.add_moment("file:T", 10, 5, {"z": {"audio": 5}, "window": [0, 10]}, "extracted")
    cid = orch.db.execute("INSERT INTO clips(moment_id,stream_key,raw_path,status,created_at) VALUES(?,?,?,?,?)",
                          (mid, "file:T", str(raw), "extracted", time.time()))
    return orch, cid


def test_whisper_failure_is_flagged_and_held(tmp_path):
    orch, cid = make_clip(tmp_path)

    class Broken:
        def words(self, media):
            raise RuntimeError("model.bin is truncated")
    orch.processor.transcriber = Broken()
    res = orch.processor.process(cid)
    clip = orch.db.one("SELECT * FROM clips WHERE id=?", (cid,))
    job = orch.db.one("SELECT * FROM publish_jobs WHERE clip_id=?", (cid,))
    assert res["status"] == "caption_failed" and clip["status"] == "caption_failed"
    assert "truncated" in clip["error"] and Path(clip["vertical_path"]).exists()
    assert job["status"] == "held"
    assert not list((orch.cfg.data_dir / "outbox").glob("*.mp4"))         # nothing published
    assert orch.db.one("SELECT level FROM events WHERE source='transcribe'")["level"] == "error"


def test_whisper_success_burns_captions(tmp_path):
    from clipper.transcribe import Word
    orch, cid = make_clip(tmp_path)

    class Ok:
        def words(self, media):
            return [Word(0.5, 0.9, "watch"), Word(1.0, 1.4, "this"), Word(1.5, 2.0, "jump")], "en"
    orch.processor.transcriber = Ok()
    assert orch.processor.process(cid)["status"] == "done"
    clip = orch.db.one("SELECT * FROM clips WHERE id=?", (cid,))
    assert clip["status"] == "dry_run" and clip["transcript"] == "watch this jump"
    assert "WATCH THIS JUMP" in Path(clip["srt_path"]).read_text(encoding="utf-8")


# ---------------------------------------------------------------- Twitch
def twitch_mock(valid=True, live=True):
    def h(req: httpx.Request):
        if req.url.path == "/oauth2/token":
            if not valid:
                return httpx.Response(403, json={"status": 403, "message": "invalid client secret"})
            return httpx.Response(200, json={"access_token": "app", "expires_in": 5000000})
        if req.url.path == "/oauth2/validate":
            return httpx.Response(200, json={"scopes": ["clips:edit"]})
        if req.url.path == "/helix/games":
            names = req.url.params.get_list("name")
            return httpx.Response(200, json={"data": [{"id": "1", "name": n} for n in names if n == "Grand Theft Auto VI"]})
        if req.url.path == "/helix/users":
            return httpx.Response(200, json={"data": [{"id": "42", "login": "good"}]})
        if req.url.path == "/helix/streams":
            data = [{"user_login": "good", "user_id": "42", "title": "t", "viewer_count": 9,
                     "game_name": "Grand Theft Auto VI"}] if live else []
            return httpx.Response(200, json={"data": data})
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(h))


def test_twitch_check_full_flow(monkeypatch, capsys):
    from clipper.twitch_setup import twitch_check
    monkeypatch.setenv("TWITCH_CLIENT_ID", "a" * 30)
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "b" * 30)
    monkeypatch.setenv("TWITCH_USER_TOKEN", "c" * 30)
    cfg = load_config(overrides={"authorized_channels": [{"platform": "twitch", "channel": "good", "authorized": True},
                                                         {"platform": "twitch", "channel": "typo", "authorized": True}]})
    rc = twitch_check(cfg, http=twitch_mock())
    out = capsys.readouterr().out
    assert "LIVE, 9 viewers" in out and "no such Twitch user" in out and "has clips:edit" in out
    assert "a" * 30 not in out and "b" * 30 not in out and rc == 1   # typo channel is a problem; secrets never printed


def test_twitch_check_rejects_bad_secret(monkeypatch, capsys):
    from clipper.twitch_setup import twitch_check
    monkeypatch.setenv("TWITCH_CLIENT_ID", "a" * 30)
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "b" * 30)
    assert twitch_check(load_config(), http=twitch_mock(valid=False)) == 1
    assert "invalid client secret" in capsys.readouterr().out


def test_credentials_prompt_writes_env_without_echo(tmp_path, monkeypatch, capsys):
    from clipper import twitch_setup as ts
    env = tmp_path / ".env"
    env.write_text("DASHBOARD_TOKEN=keepme\nTWITCH_CLIENT_ID=\nTWITCH_CLIENT_SECRET=\n", encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda prompt="": "abcdefghij0123456789abcdef")
    monkeypatch.setattr(ts.getpass, "getpass", lambda prompt="": "zyxwvutsrq9876543210zyxwvu")
    monkeypatch.setattr(ts, "validate_app_credentials", lambda c, s, http=None: (True, "credentials valid"))
    monkeypatch.delenv("TWITCH_CLIENT_ID", raising=False)
    monkeypatch.delenv("TWITCH_CLIENT_SECRET", raising=False)
    assert ts.prompt_twitch_credentials(env) == 0
    text = env.read_text(encoding="utf-8")
    assert "DASHBOARD_TOKEN=keepme" in text and "TWITCH_CLIENT_SECRET=zyxwvutsrq9876543210zyxwvu" in text
    assert text.count("TWITCH_CLIENT_ID=") == 1
    assert "zyxwvutsrq9876543210zyxwvu" not in capsys.readouterr().out
    if __import__("os").name != "nt":
        assert oct(env.stat().st_mode & 0o777) == "0o600"


def test_env_credentials_reach_app(tmp_path, monkeypatch):
    from clipper import config
    env = tmp_path / ".env"
    env.write_text("﻿TWITCH_CLIENT_ID=fromenvfile12345678901\n", encoding="utf-8")
    monkeypatch.delenv("TWITCH_CLIENT_ID", raising=False)
    config.load_dotenv(env)
    assert config.secret("TWITCH_CLIENT_ID") == "fromenvfile12345678901"
