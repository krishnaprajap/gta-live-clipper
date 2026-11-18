"""Dry-run publisher: writes the final video + metadata into an outbox
folder exactly as it would be uploaded. Also usable as the hand-off folder
for an external editor such as a self-hosted OpenShorts instance."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .base import PublishResult, Publisher


class DryRunPublisher(Publisher):
    name = "dry_run"

    def __init__(self, outbox: Path):
        self.outbox = Path(outbox)
        self.outbox.mkdir(parents=True, exist_ok=True)

    def publish(self, video: Path, meta: dict) -> PublishResult:
        dst = self.outbox / Path(video).name
        shutil.copy2(video, dst)
        dst.with_suffix(".json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        return PublishResult(True, "dry_run", remote_id=dst.name, remote_url=str(dst))
