"""End-to-end: synthetic recording played back in real time as a 'live'
input -> rolling buffer -> detection -> extraction -> vertical edit ->
metadata -> dry-run publish. Takes ~1 minute."""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from clipper.config import load_config
from clipper.ffmpeg_utils import probe
from clipper.pipeline import Orchestrator, StreamSpec

ROOT = Path(__file__).resolve().parent.parent


def test_end_to_end(tmp_path):
    src = tmp_path / "stream.mp4"
    subprocess.run([sys.executable, str(ROOT / "scripts/make_test_stream.py"), "--out", str(src),
                    "--duration", "50", "--event", "32"], check=True)
    cfg = load_config(overrides={"data_dir": str(tmp_path / "data"), "transcription": {"enabled": False},
                                 "publish": {"mode": "dry_run"}})
    orch = Orchestrator(cfg)
    asyncio.run(orch.run_single(StreamSpec("file", "E2E", str(src), chat_replay=str(tmp_path / "chat.jsonl"))))

    moments = orch.db.query("SELECT * FROM moments")
    assert len(moments) == 1
    assert 30 <= moments[0]["t"] <= 42  # the event is at 32-38 s
    clip = orch.db.one("SELECT * FROM clips")
    assert clip["status"] == "dry_run", clip
    v = probe(clip["vertical_path"])
    vs = [s for s in v["streams"] if s["codec_type"] == "video"][0]
    assert (vs["width"], vs["height"]) == (1080, 1920)
    assert any(s["codec_type"] == "audio" for s in v["streams"])
    window = json.loads(moments[0]["signals"])["window"]
    assert window[0] <= 32 and window[1] >= 36
    out = Path(orch.db.one("SELECT remote_url FROM publish_jobs")["remote_url"])
    assert out.exists() and json.loads(out.with_suffix(".json").read_text())["title"]
    assert not (cfg.data_dir / "buffers" / "file_E2E").exists()  # buffer cleaned up


def test_fingerprint_detects_reencoded_duplicate(tmp_path):
    from clipper.quality import fingerprint, similarity
    src = tmp_path / "s.mp4"
    subprocess.run([sys.executable, str(ROOT / "scripts/make_test_stream.py"), "--out", str(src),
                    "--duration", "40", "--event", "20"], check=True)
    def cut(ss, name, crf):
        p = tmp_path / name
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", str(ss), "-t", "12", "-i", str(src),
                        "-vf", "scale=854:480", "-c:v", "libx264", "-crf", str(crf), str(p)], check=True)
        return fingerprint(p)
    a, a2, b = cut(16, "a.mp4", 18), cut(16, "a2.mp4", 35), cut(2, "b.mp4", 18)
    assert similarity(a, a2) > 0.9
    assert similarity(a, b) < 0.9
