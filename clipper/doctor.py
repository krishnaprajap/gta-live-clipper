"""Pre-flight checks for a deployment. Prints only whether secrets are set,
never their values. Exit code 1 if anything required is missing."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .config import Config, secret


def _req(cfg: Config) -> list[tuple[str, str, bool]]:
    """(variable, why, required_for_current_config)"""
    mode = cfg["publish"]["mode"]
    auth = [c for c in cfg["authorized_channels"] if c.get("authorized")]
    yt_auth = any(c["platform"] == "youtube" for c in auth)
    tw_auth = any(c["platform"] == "twitch" for c in auth)
    return [
        ("DASHBOARD_TOKEN", "dashboard login (serve)", True),
        ("TWITCH_CLIENT_ID", "Twitch live watchdog: auto start/reconnect + discovery", tw_auth),
        ("TWITCH_CLIENT_SECRET", "Twitch live watchdog: auto start/reconnect + discovery", tw_auth),
        ("TWITCH_USER_TOKEN", "publish.mode=twitch_clip (clips:edit)", mode == "twitch_clip"),
        ("YOUTUBE_API_KEY", "YouTube discovery + live chat", yt_auth),
        ("YOUTUBE_CLIENT_ID", "publish.mode=youtube", mode == "youtube"),
        ("YOUTUBE_CLIENT_SECRET", "publish.mode=youtube", mode == "youtube"),
        ("YOUTUBE_REFRESH_TOKEN", "publish.mode=youtube", mode == "youtube"),
        ("ANTHROPIC_API_KEY", "metadata.use_llm=true", bool(cfg["metadata"].get("use_llm"))),
    ]


def run_doctor(cfg: Config) -> int:
    from .dashboard.app import token_problem
    bad = 0

    def line(status, name, note=""):
        print(f"[{status:4}] {name:24} {note}")

    print("== environment variables (values are never printed) ==")
    for var, why, required in _req(cfg):
        is_set = bool(secret(var))
        if var == "DASHBOARD_TOKEN" and is_set and token_problem(secret(var)):
            line("FAIL", var, token_problem(secret(var)))
            bad += 1
        elif is_set:
            line("OK", var, f"set — {why}")
        elif required:
            line("FAIL", var, f"missing — required: {why}")
            bad += 1
        else:
            line("--", var, f"not set — optional: {why}")
    if cfg["publish"]["mode"] == "twitch_clip" and not (secret("TWITCH_CLIENT_ID") and secret("TWITCH_CLIENT_SECRET")):
        line("FAIL", "TWITCH_CLIENT_ID/SECRET", "required by publish.mode=twitch_clip")
        bad += 1

    print("== binaries ==")
    from .ffmpeg_utils import FFMPEG, FFPROBE
    from .system import hardware, streamlink_available
    for b, path in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE)):
        ok = bool(shutil.which(path) or Path(path).exists())
        line("OK" if ok else "FAIL", b, path if ok else "not found (install FFmpeg or set FFMPEG_BINARY/FFPROBE_BINARY in .env)")
        bad += 0 if ok else 1
    sl = streamlink_available()
    line("OK" if sl else "FAIL", "streamlink", "python module available" if sl else "pip install streamlink")
    bad += 0 if sl else 1
    try:
        import ctranslate2
        import faster_whisper  # noqa: F401
        types = ctranslate2.get_supported_compute_types("cpu")
        want = cfg["transcription"]["compute_type"]
        line("OK" if want in types else "FAIL", "faster-whisper", f"cpu compute types {sorted(types)}; configured {want}")
        bad += 0 if want in types else 1
    except Exception as e:
        line("FAIL", "faster-whisper", str(e))
        bad += 1
    try:  # the exact call faster-whisper uses to read audio (catches PyAV incompatibilities)
        import tempfile
        import wave
        import av
        from faster_whisper.audio import decode_audio
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "probe.wav"
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b"\0\0" * 8000)
            decode_audio(str(wav))
        line("OK", "whisper audio decoding", f"PyAV {av.__version__}")
    except Exception as e:
        line("FAIL", "whisper audio decoding", f"{type(e).__name__}: {e} -> pip install -r requirements.txt")
        bad += 1
    if cfg["transcription"]["enabled"]:
        from .whisper_model import status as wstatus
        st = wstatus(cfg["transcription"]["model"], check_remote=False)
        line("OK" if not st["problems"] else "FAIL", f"whisper model '{cfg['transcription']['model']}'",
             "complete" if not st["problems"] else "; ".join(st["problems"]) + " -> python -m clipper whisper repair")
        bad += 1 if st["problems"] else 0

    print("== config ==")
    auth = [f"{c['platform']}:{c['channel']}" for c in cfg["authorized_channels"] if c.get("authorized")]
    line("OK" if auth else "WARN", "authorized_channels", ", ".join(auth) or "none authorized — nothing will be captured")
    line("OK", "publish.mode", cfg["publish"]["mode"])
    host = os.environ.get("DASHBOARD_HOST", cfg["dashboard"]["host"])
    in_docker = os.path.exists("/.dockerenv")
    hw = hardware()
    line("OK", "hardware", f"{hw['logical_cores']} logical cores, {hw['ram_gb']} GB RAM -> max_streams={cfg.max_streams}"
         f" (config: {cfg['discovery'].get('max_streams')})")
    line("OK", "data_dir", str(cfg.data_dir))
    exposed = host not in ("127.0.0.1", "localhost") and not in_docker
    line("WARN" if exposed else "OK", "dashboard bind", f"{host}" + (" (publicly reachable unless firewalled)" if exposed else ""))
    free = shutil.disk_usage(cfg.data_dir).free / 1024 ** 3
    need = cfg["storage"]["max_disk_gb"] + 2
    line("OK" if free >= need else "WARN", "disk free", f"{free:.1f} GB (storage.max_disk_gb={cfg['storage']['max_disk_gb']})")
    line("OK" if hw["ram_gb"] >= 7.5 else "WARN", "RAM", f"{hw['ram_gb']} GB" + ("" if hw["ram_gb"] >= 7.5 else " (8 GB+ recommended on a desktop PC)"))
    print(f"== {'all required checks passed' if not bad else f'{bad} problem(s)'} ==")
    return 1 if bad else 0
