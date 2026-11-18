"""YouTube Data API v3 discovery.

search.list costs 100 quota units per call (default project quota is
10,000/day), so discovery is rate-limited by the caller. videos.list costs 1.
"""
from __future__ import annotations

import httpx

from .twitch import StreamInfo

API = "https://www.googleapis.com/youtube/v3"


class YouTubeAPI:
    def __init__(self, api_key: str, http: httpx.Client | None = None, on_quota=None):
        self.key = api_key
        self.http = http or httpx.Client(timeout=20)
        self.on_quota = on_quota

    def _get(self, path: str, params: dict, units: int) -> dict:
        r = self.http.get(f"{API}{path}", params={**params, "key": self.key})
        if self.on_quota:
            self.on_quota(units)
        r.raise_for_status()
        return r.json()

    def live_streams(self, query: str = "GTA 6", max_results: int = 25, channel_ids: list[str] | None = None,
                     language: str | None = None) -> list[StreamInfo]:
        """If channel_ids is given, search only those channels (still 100 units each),
        otherwise a global live search for the query."""
        video_ids: list[str] = []
        targets = channel_ids or [None]
        for cid in targets:
            params = {"part": "snippet", "eventType": "live", "type": "video", "q": query,
                      "maxResults": max_results, "videoCategoryId": "20"}
            if cid:
                params["channelId"] = cid
            if language:
                params["relevanceLanguage"] = language
            data = self._get("/search", params, 100)
            video_ids += [it["id"]["videoId"] for it in data.get("items", []) if it.get("id", {}).get("videoId")]
        return self.videos(video_ids)

    def videos(self, video_ids: list[str]) -> list[StreamInfo]:
        out = []
        for i in range(0, len(video_ids), 50):
            data = self._get("/videos", {"part": "snippet,liveStreamingDetails", "id": ",".join(video_ids[i:i + 50])}, 1)
            for v in data.get("items", []):
                live = v.get("liveStreamingDetails", {})
                if not live or live.get("actualEndTime"):
                    continue
                sn = v["snippet"]
                out.append(StreamInfo(platform="youtube", channel=sn.get("channelTitle", ""), title=sn.get("title", ""),
                                      viewers=int(live.get("concurrentViewers", 0) or 0),
                                      url=f"https://www.youtube.com/watch?v={v['id']}",
                                      language=sn.get("defaultAudioLanguage", ""), started_at=live.get("actualStartTime", ""),
                                      channel_id=sn.get("channelId", ""), live_chat_id=live.get("activeLiveChatId", ""),
                                      video_id=v["id"]))
        return out
