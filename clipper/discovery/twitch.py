"""Twitch Helix discovery + Create Clip.

Endpoints used (all official Helix API):
  POST https://id.twitch.tv/oauth2/token           (app access token, client credentials)
  GET  https://api.twitch.tv/helix/games?name=...   (resolve GTA VI category id)
  GET  https://api.twitch.tv/helix/streams?game_id=...
  GET  https://api.twitch.tv/helix/users?login=...
  POST https://api.twitch.tv/helix/clips?broadcaster_id=...  (user token, clips:edit)
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

HELIX = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"


@dataclass
class StreamInfo:
    platform: str
    channel: str
    title: str
    viewers: int
    url: str
    language: str = ""
    started_at: str = ""
    channel_id: str = ""
    live_chat_id: str = ""   # YouTube only
    video_id: str = ""       # YouTube only
    authorized: bool = False


class TwitchAPI:
    def __init__(self, client_id: str, client_secret: str, http: httpx.Client | None = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.http = http or httpx.Client(timeout=20)
        self._token: str | None = None
        self._token_exp = 0.0
        self._game_ids: dict[str, str] = {}

    def _app_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = self.http.post(TOKEN_URL, data={"client_id": self.client_id, "client_secret": self.client_secret,
                                            "grant_type": "client_credentials"})
        r.raise_for_status()
        d = r.json()
        self._token, self._token_exp = d["access_token"], time.time() + d.get("expires_in", 3600)
        return self._token

    def _get(self, path: str, params) -> dict:
        r = self.http.get(f"{HELIX}{path}", params=params,
                          headers={"Client-Id": self.client_id, "Authorization": f"Bearer {self._app_token()}"})
        r.raise_for_status()
        return r.json()

    def game_ids(self, names: list[str]) -> list[str]:
        missing = [n for n in names if n not in self._game_ids]
        if missing:
            data = self._get("/games", [("name", n) for n in missing])
            for g in data.get("data", []):
                self._game_ids[g["name"]] = g["id"]
            for n in missing:
                self._game_ids.setdefault(n, "")
        return list(dict.fromkeys(i for n in names if (i := self._game_ids.get(n))))

    def live_streams(self, game_names: list[str], languages: list[str] | None = None, limit: int = 100) -> list[StreamInfo]:
        ids = self.game_ids(game_names)
        if not ids:
            return []
        params = [("game_id", i) for i in ids] + [("first", str(min(limit, 100))), ("type", "live")]
        params += [("language", l) for l in (languages or [])]
        out = []
        for s in self._get("/streams", params).get("data", []):
            out.append(StreamInfo(platform="twitch", channel=s["user_login"], title=s.get("title", ""),
                                  viewers=int(s.get("viewer_count", 0)), url=f"https://www.twitch.tv/{s['user_login']}",
                                  language=s.get("language", ""), started_at=s.get("started_at", ""),
                                  channel_id=s["user_id"]))
        return out

    def user_id(self, login: str) -> str | None:
        d = self._get("/users", {"login": login}).get("data", [])
        return d[0]["id"] if d else None

    def create_clip(self, broadcaster_id: str, user_token: str, has_delay: bool = False) -> dict:
        """Creates a native Twitch clip of the *live* broadcast (last ~30 s).
        Requires a user token with clips:edit. Returns {'id', 'edit_url'}."""
        r = self.http.post(f"{HELIX}/clips", params={"broadcaster_id": broadcaster_id, "has_delay": str(has_delay).lower()},
                           headers={"Client-Id": self.client_id, "Authorization": f"Bearer {user_token}"})
        r.raise_for_status()
        return r.json()["data"][0]
