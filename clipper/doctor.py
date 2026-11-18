"""Pre-flight checks for a deployment. Prints only whether secrets are set,
never their values. Exit code 1 if anything required is missing."""
from __future__ import annotations

import os
import shutil
import subprocess

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
    for b in ("ffmpeg", "ffprobe", "streamlink"):
        path = shutil.which(b)
        line("OK" if path else "FAIL", b, path or "not found")
        bad += 0 if path else 1
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

    print("== config ==")
    auth = [f"{c['platform']}:{c['channel']}" for c in cfg["authorized_channels"] if c.get("authorized")]
    line("OK" if auth else "WARN", "authorized_channels", ", ".join(auth) or "none authorized — nothing will be captured")
    line("OK", "publish.mode", cfg["publish"]["mode"])
    host = os.environ.get("DASHBOARD_HOST", cfg["dashboard"]["host"])
    in_docker = os.path.exists("/.dockerenv")
    exposed = host not in ("127.0.0.1", "localhost") and not in_docker
    line("WARN" if exposed else "OK", "dashboard bind", f"{host}" + (" (publicly reachable unless firewalled)" if exposed else ""))
    free = shutil.disk_usage(cfg.data_dir).free / 1024 ** 3
    need = cfg["storage"]["max_disk_gb"] + 2
    line("OK" if free >= need else "WARN", "disk free", f"{free:.1f} GB (storage.max_disk_gb={cfg['storage']['max_disk_gb']})")
    try:
        mem = int(open("/proc/meminfo").read().split()[1]) / 1024 ** 2
        line("OK" if mem >= 3.5 else "WARN", "RAM", f"{mem:.1f} GB")
    except OSError:
        pass
    print(f"== {'all required checks passed' if not bad else f'{bad} problem(s)'} ==")
    return 1 if bad else 0
