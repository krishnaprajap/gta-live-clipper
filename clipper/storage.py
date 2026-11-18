"""Disk usage limits and retention cleanup."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from .db import DB


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def cleanup(db: DB, data_dir: Path, max_disk_gb: float, keep_clips_days: float, keep_published_days: float) -> dict:
    now = time.time()
    removed = 0
    clips_dir = data_dir / "clips"
    rows = db.query("SELECT c.id, c.raw_path, c.vertical_path, c.srt_path, c.status, c.created_at FROM clips c "
                    "ORDER BY c.created_at ASC")
    for r in rows:
        age_days = (now - (r["created_at"] or now)) / 86400
        limit = keep_published_days if r["status"] == "published" else keep_clips_days
        if age_days > limit:
            removed += _remove_files(r)
            db.update("clips", r["id"], status=r["status"] if r["status"] == "published" else "expired")
    # hard cap: delete oldest clip files until under quota
    cap = max_disk_gb * 1024 ** 3
    if clips_dir.exists():
        for r in rows:
            if dir_size(data_dir) <= cap:
                break
            removed += _remove_files(r)
    free_gb = shutil.disk_usage(data_dir).free / 1024 ** 3
    return {"removed_files": removed, "data_gb": round(dir_size(data_dir) / 1024 ** 3, 3), "free_gb": round(free_gb, 1)}


def _remove_files(row: dict) -> int:
    n = 0
    for k in ("raw_path", "vertical_path", "srt_path"):
        p = row.get(k)
        if p and Path(p).exists():
            Path(p).unlink()
            n += 1
    return n
