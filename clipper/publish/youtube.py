"""YouTube upload via the official Data API v3 resumable upload protocol.

Notes (real platform constraints, not code limitations):
* Needs an OAuth client + refresh token with the youtube.upload scope
  (scripts/youtube_auth.py obtains one).
* Videos uploaded by API projects that have not passed Google's API
  compliance audit are locked to *private*. Request the audit before
  expecting public uploads.
* videos.insert costs ~1,600 quota units; default quota is 10,000/day.
* YouTube decides whether a vertical video <= 3 min is shown as a Short.
"""
from __future__ import annotations

import time
from pathlib import Path

import httpx

from .base import PublishResult, Publisher

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"


class YouTubePublisher(Publisher):
    name = "youtube"

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, privacy: str = "private",
                 category_id: str = "20", http: httpx.Client | None = None, chunk_mb: int = 8):
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self.privacy, self.category_id = privacy, category_id
        self.http = http or httpx.Client(timeout=120)
        self.chunk = chunk_mb * 1024 * 1024
        self._token, self._exp = None, 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._exp - 60:
            return self._token
        r = self.http.post(TOKEN_URL, data={"client_id": self.client_id, "client_secret": self.client_secret,
                                            "refresh_token": self.refresh_token, "grant_type": "refresh_token"})
        r.raise_for_status()
        d = r.json()
        self._token, self._exp = d["access_token"], time.time() + d.get("expires_in", 3600)
        return self._token

    def publish(self, video: Path, meta: dict) -> PublishResult:
        video = Path(video)
        size = video.stat().st_size
        body = {
            "snippet": {"title": meta["title"][:100], "description": meta["description"][:5000],
                        "tags": meta.get("tags", [])[:30], "categoryId": self.category_id},
            "status": {"privacyStatus": self.privacy, "selfDeclaredMadeForKids": False,
                       "containsSyntheticMedia": False},
        }
        try:
            headers = {"Authorization": f"Bearer {self._access_token()}", "X-Upload-Content-Type": "video/mp4",
                       "X-Upload-Content-Length": str(size)}
            r = self.http.post(UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                               json=body, headers=headers)
            if r.status_code != 200:
                return PublishResult(False, "failed", error=f"init HTTP {r.status_code}: {r.text[:300]}")
            session = r.headers["Location"]
            offset = 0
            with open(video, "rb") as f:
                while offset < size:
                    f.seek(offset)
                    data = f.read(self.chunk)
                    end = offset + len(data) - 1
                    r = self.http.put(session, content=data, headers={
                        "Authorization": f"Bearer {self._access_token()}",
                        "Content-Length": str(len(data)), "Content-Range": f"bytes {offset}-{end}/{size}"})
                    if r.status_code in (200, 201):
                        vid = r.json()["id"]
                        return PublishResult(True, "published", remote_id=vid, remote_url=f"https://youtube.com/shorts/{vid}")
                    if r.status_code == 308:
                        rng = r.headers.get("Range")
                        offset = int(rng.split("-")[1]) + 1 if rng else 0
                        continue
                    return PublishResult(False, "failed", error=f"upload HTTP {r.status_code}: {r.text[:300]}")
            return PublishResult(False, "failed", error="upload ended without a video id")
        except (httpx.HTTPError, KeyError) as e:
            return PublishResult(False, "failed", error=f"{type(e).__name__}: {e}")
