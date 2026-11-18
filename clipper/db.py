"""SQLite state store: streams, moments, clips, publish jobs, spend ledger."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS streams (
  id INTEGER PRIMARY KEY, platform TEXT, channel TEXT, title TEXT, viewers INTEGER,
  url TEXT, status TEXT, started_at REAL, updated_at REAL, error TEXT,
  UNIQUE(platform, channel)
);
CREATE TABLE IF NOT EXISTS moments (
  id INTEGER PRIMARY KEY, stream_key TEXT, t REAL, score REAL, signals TEXT,
  status TEXT, reason TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS clips (
  id INTEGER PRIMARY KEY, moment_id INTEGER, stream_key TEXT, raw_path TEXT,
  vertical_path TEXT, srt_path TEXT, duration REAL, fingerprint TEXT,
  title TEXT, description TEXT, tags TEXT, transcript TEXT,
  status TEXT, error TEXT, created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS publish_jobs (
  id INTEGER PRIMARY KEY, clip_id INTEGER, target TEXT, status TEXT,
  remote_id TEXT, remote_url TEXT, error TEXT, attempts INTEGER DEFAULT 0,
  created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS spend (
  id INTEGER PRIMARY KEY, category TEXT, amount_inr REAL, note TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, level TEXT, source TEXT, message TEXT, created_at REAL
);
"""


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.lastrowid

    def query(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # convenience helpers -------------------------------------------------
    def log(self, level: str, source: str, message: str) -> None:
        self.execute("INSERT INTO events(level,source,message,created_at) VALUES(?,?,?,?)",
                     (level, source, message[:2000], time.time()))

    def upsert_stream(self, platform: str, channel: str, **fields: Any) -> None:
        now = time.time()
        existing = self.one("SELECT id FROM streams WHERE platform=? AND channel=?", (platform, channel))
        if existing:
            sets = ", ".join(f"{k}=?" for k in fields) + (", " if fields else "") + "updated_at=?"
            self.execute(f"UPDATE streams SET {sets} WHERE id=?", (*fields.values(), now, existing["id"]))
        else:
            cols = ["platform", "channel", *fields.keys(), "updated_at"]
            self.execute(f"INSERT INTO streams({','.join(cols)}) VALUES({','.join('?'*len(cols))})",
                         (platform, channel, *fields.values(), now))

    def add_moment(self, stream_key: str, t: float, score: float, signals: dict, status: str, reason: str = "") -> int:
        return self.execute(
            "INSERT INTO moments(stream_key,t,score,signals,status,reason,created_at) VALUES(?,?,?,?,?,?,?)",
            (stream_key, t, score, json.dumps(signals), status, reason, time.time()))

    def update(self, table: str, row_id: int, **fields: Any) -> None:
        if table not in {"streams", "moments", "clips", "publish_jobs"}:
            raise ValueError(table)
        fields["updated_at"] = time.time() if table != "moments" else None
        if table == "moments":
            fields.pop("updated_at")
        sets = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE {table} SET {sets} WHERE id=?", (*fields.values(), row_id))
