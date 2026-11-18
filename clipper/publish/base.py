from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class PublishResult:
    ok: bool
    status: str            # published | dry_run | failed | skipped
    remote_id: str = ""
    remote_url: str = ""
    error: str = ""


class Publisher:
    name = "base"

    def publish(self, video: Path, meta: dict) -> PublishResult:  # pragma: no cover - interface
        raise NotImplementedError
