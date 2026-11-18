"""Caption verification fixture: an isolated run on the speech test video must
yield one non-duplicate clip with a transcript, an SRT and burned-in captions.
(The real Whisper model cannot be downloaded in CI; a stand-in transcriber
returns the spoken sentence. Everything else is the real pipeline.)"""
import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

from clipper.config import load_config
from clipper.pipeline import Orchestrator, StreamSpec
from clipper.transcribe import Word
from clipper.whisper_cli import _verify_latest_clip, captions_burned

ROOT = Path(__file__).resolve().parent.parent
SENTENCE = "Grand Theft Auto six is finally here. Watch this car jump over the bridge."


class SpeechTranscriber:
    """Returns the sentence with timings where add_speech.py placed it."""
    def __init__(self, offset):
        self.offset = offset

    def words(self, media):
        return [Word(self.offset + i * 0.4, self.offset + i * 0.4 + 0.35, w) for i, w in enumerate(SENTENCE.split())], "en"


def test_speech_fixture_end_to_end(tmp_path, capsys):
    video, speech = tmp_path / "stream.mp4", tmp_path / "speech.wav"
    subprocess.run([sys.executable, str(ROOT / "scripts/make_test_stream.py"), "--out", str(video),
                    "--duration", "60", "--event", "40"], check=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=f=300:duration=6", str(speech)], check=True)
    sv = tmp_path / "stream_speech.mp4"
    subprocess.run([sys.executable, str(ROOT / "scripts/add_speech.py"), "--video", str(video), "--speech", str(speech),
                    "--at", "26", "--out", str(sv)], check=True)

    cfg = load_config(overrides={"data_dir": str(tmp_path / "verify-captions"), "publish": {"mode": "dry_run"}})
    orch = Orchestrator(cfg)
    orch.processor.transcriber = SpeechTranscriber(offset=4.0)
    asyncio.run(orch.run_single(StreamSpec("file", "CaptionTest", str(sv), chat_replay=str(tmp_path / "chat.jsonl"))))

    clips = orch.db.query("SELECT * FROM clips")
    assert len(clips) == 1 and clips[0]["status"] == "dry_run", clips   # one independent clip, not a duplicate
    assert _verify_latest_clip(cfg, SENTENCE) == 0
    out = capsys.readouterr().out
    assert "[PASS] captions burned into vertical video" in out and "RESULT: PASS" in out

    # negative control: a vertical video WITHOUT burned captions must fail the check
    from clipper.edit import make_vertical
    c = clips[0]
    nosubs = tmp_path / "nosubs.mp4"
    make_vertical(Path(c["raw_path"]), nosubs, srt=None)
    ok, detail = captions_burned(Path(c["raw_path"]), nosubs, Path(c["srt_path"]), cfg)
    assert not ok, detail


def test_overlap_only_within_same_capture_session(tmp_path):
    cfg = load_config(overrides={"data_dir": str(tmp_path), "transcription": {"enabled": False}})
    orch = Orchestrator(cfg)
    db = orch.db
    m = db.add_moment("twitch:x", 58, 9, {"window": [38, 72], "session": "twitch_x_1000"}, "extracted")
    db.execute("INSERT INTO clips(moment_id,stream_key,raw_path,status,fingerprint,created_at) VALUES(?,?,?,?,?,strftime('%s','now'))",
               (m, "twitch:x", "a.mp4", "caption_failed", ""))
    p = orch.processor
    # same session, same window -> duplicate (dedupe still active, caption_failed clips still count)
    assert p.is_duplicate(99, "twitch:x", (40, 70), "", "twitch_x_1000").startswith("overlaps clip")
    # after a reconnect capture time restarts at 0: same numbers, different footage -> not a duplicate
    assert p.is_duplicate(99, "twitch:x", (40, 70), "", "twitch_x_2000") is None
