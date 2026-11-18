from __future__ import annotations

from pathlib import Path

from ..config import secret
from .base import PublishResult, Publisher
from .dryrun import DryRunPublisher


def make_publisher(cfg, data_dir: Path) -> Publisher:
    mode = cfg["publish"]["mode"]
    if mode == "youtube":
        keys = [secret("YOUTUBE_CLIENT_ID"), secret("YOUTUBE_CLIENT_SECRET"), secret("YOUTUBE_REFRESH_TOKEN")]
        if not all(keys):
            raise RuntimeError("publish.mode=youtube needs YOUTUBE_CLIENT_ID/SECRET/REFRESH_TOKEN in the environment")
        from .youtube import YouTubePublisher
        return YouTubePublisher(*keys, privacy=cfg["publish"]["youtube_privacy"],
                                category_id=cfg["publish"]["youtube_category_id"])
    return DryRunPublisher(data_dir / "outbox")


__all__ = ["make_publisher", "Publisher", "PublishResult", "DryRunPublisher"]
