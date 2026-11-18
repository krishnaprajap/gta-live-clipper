"""Configuration loading. Secrets come only from environment variables."""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATHS = [ROOT / "config" / "config.yaml", ROOT / "config" / "config.example.yaml"]

SECRET_KEYS = [
    "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "TWITCH_USER_TOKEN",
    "YOUTUBE_API_KEY", "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN",
    "ANTHROPIC_API_KEY", "DASHBOARD_TOKEN",
]


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal .env loader (does not override already-set env vars)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def secret(name: str) -> str | None:
    v = os.environ.get(name)
    return v or None


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Config:
    raw: dict[str, Any]
    path: Path | None = None

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    @property
    def data_dir(self) -> Path:
        p = Path(self.raw.get("data_dir", "./data"))
        if not p.is_absolute():
            p = ROOT / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    def authorized(self, platform: str, channel: str) -> bool:
        for c in self.raw.get("authorized_channels", []):
            if c.get("platform") == platform and str(c.get("channel", "")).lower() == channel.lower():
                return bool(c.get("authorized"))
        return False

    def redacted(self) -> dict:
        """Config safe to show in the dashboard; secrets reported only as set/unset."""
        out = copy.deepcopy(self.raw)
        out["secrets"] = {k: bool(secret(k)) for k in SECRET_KEYS}
        return out


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> Config:
    load_dotenv()
    example = yaml.safe_load((ROOT / "config" / "config.example.yaml").read_text())
    chosen = Path(path) if path else next((p for p in DEFAULT_PATHS if p.exists()), None)
    raw = example
    if chosen and chosen.exists() and chosen.name != "config.example.yaml":
        raw = _merge(example, yaml.safe_load(chosen.read_text()) or {})
    if overrides:
        raw = _merge(raw, overrides)
    return Config(raw=raw, path=chosen)
