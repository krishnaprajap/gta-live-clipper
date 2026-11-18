"""Rank discovered streams and select which to capture.

Only streams on the authorized allowlist are eligible for capture. Others
are reported in the dashboard as 'discovered (not authorized)' so the
operator can request permission from the creator."""
from __future__ import annotations

import math
import re

from .twitch import StreamInfo

GTA6_RX = re.compile(r"\bgta\s*(6|vi)\b|grand theft auto\s*(6|vi)", re.I)


def score(s: StreamInfo) -> float:
    v = math.log10(max(s.viewers, 1) + 1)
    title_bonus = 0.5 if GTA6_RX.search(s.title or "") else 0.0
    return round(v + title_bonus, 3)


def select(streams: list[StreamInfo], cfg, max_streams: int) -> tuple[list[StreamInfo], list[StreamInfo]]:
    dcfg = cfg["discovery"]
    for s in streams:
        key = s.channel_id if s.platform == "youtube" else s.channel
        s.authorized = cfg.authorized(s.platform, key) or cfg.authorized(s.platform, s.channel)
    eligible = [s for s in streams if s.viewers >= dcfg.get("min_viewers", 0) or s.authorized]
    if s_langs := dcfg.get("languages"):
        eligible = [s for s in eligible if not s.language or s.language.split("-")[0] in s_langs]
    if any(s.platform == "youtube" for s in eligible):
        eligible = [s for s in eligible if s.platform != "youtube" or GTA6_RX.search(s.title or "")]
    ranked = sorted(eligible, key=score, reverse=True)
    chosen = [s for s in ranked if s.authorized][:max_streams]
    return chosen, ranked
