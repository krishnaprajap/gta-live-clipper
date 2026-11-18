"""Title/description/tag generation.

Default: free, deterministic templates driven by the transcript and the
detection signals. Optional: a single short Claude call per clip, only when
enabled in config, an API key is present, and the hard monthly AI budget
has room (spend is reserved *before* the call)."""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field

from .budget import Budget, BudgetExceeded
from .config import secret

STOP = set("""a an the and or but if then so to of in on at for with is are was were be been it its this that
i you he she we they me him her us them my your our their what who how why when where not no yes oh uh um like
just got get gonna wanna yeah okay ok do did does have has had there here up down out im dont can cant will
""".split())

BASE_TAGS = ["GTA 6", "GTA VI", "Grand Theft Auto VI", "GTA 6 gameplay", "gaming", "shorts", "live stream highlights"]

REACTIONS = {
    "chat_hype": ["Chat went WILD", "Chat lost it", "Nobody expected this"],
    "audio": ["The reaction says it all", "He couldn't believe it", "Pure chaos"],
    "scene": ["This escalated fast", "Absolute mayhem", "Wait for it"],
    "chat_rate": ["Chat exploded", "Everyone saw this", "The moment chat erupted"],
}


@dataclass
class Metadata:
    title: str
    description: str
    tags: list[str] = field(default_factory=list)
    source: str = "template"

    def as_dict(self) -> dict:
        return {"title": self.title, "description": self.description, "tags": self.tags, "source": self.source}


def keywords(transcript: str, n: int = 4) -> list[str]:
    words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z']{2,}", transcript)]
    counts = Counter(w for w in words if w not in STOP)
    return [w for w, _ in counts.most_common(n)]


def template_metadata(channel: str, platform: str, transcript: str, signals: dict, credit: str,
                      source_url: str = "", seed: int = 0) -> Metadata:
    z = signals.get("z", {}) if signals else {}
    top = max(z, key=z.get) if z else "chat_rate"
    options = REACTIONS.get(top, REACTIONS["chat_rate"])
    reaction = options[seed % len(options)]
    quote = ""
    t = transcript.strip()
    if t:
        first = re.split(r"(?<=[.!?])\s+", t)[0]
        if 8 <= len(first) <= 45:
            quote = f' "{first.strip()}"'
    title = f"{reaction}{quote} | {channel} GTA 6"
    if len(title) > 95:
        title = f"{reaction} | {channel} GTA 6"
    kw = keywords(transcript)
    tags = list(dict.fromkeys(BASE_TAGS + [channel, f"{channel} GTA 6"] + kw))[:15]
    hashtags = " ".join(["#GTA6", "#GTAVI", "#shorts", "#" + re.sub(r"\W", "", channel)])
    desc_lines = [f"{reaction} — live GTA VI moment from {channel}.", "", credit]
    if source_url:
        desc_lines.append(f"Watch the full stream: {source_url}")
    desc_lines += ["", hashtags]
    return Metadata(title=title[:100], description="\n".join(desc_lines)[:4900], tags=tags)


LLM_PROMPT = """You write metadata for a short vertical gaming clip (YouTube Shorts).
Game: GTA VI. Streamer: {channel} ({platform}). Detected signals: {signals}.
Transcript of the clip (may be empty or noisy): {transcript!r}

Return ONLY a JSON object: {{"title": str (max 80 chars, punchy, no clickbait lies, no misleading claims),
"description": str (2 short sentences), "tags": [up to 10 short strings]}}"""


def llm_metadata(channel: str, platform: str, transcript: str, signals: dict, budget: Budget,
                 model: str, est_cost_inr: float) -> Metadata | None:
    """Returns None (caller falls back to templates) if disabled, unaffordable or failing."""
    if not secret("ANTHROPIC_API_KEY"):
        return None
    try:
        budget.charge("ai", est_cost_inr, f"metadata:{channel}")
    except BudgetExceeded:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(max_retries=1, timeout=30)
        resp = client.messages.create(
            model=model, max_tokens=400, output_config={"effort": "low"},
            messages=[{"role": "user", "content": LLM_PROMPT.format(
                channel=channel, platform=platform, signals=json.dumps(signals.get("z", {})),
                transcript=transcript[:1500])}])
        if resp.stop_reason == "refusal":
            return None
        text = "".join(b.text for b in resp.content if b.type == "text")
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
        return Metadata(title=str(data["title"])[:100], description=str(data["description"]),
                        tags=[str(t) for t in data.get("tags", [])][:10], source=f"llm:{model}")
    except Exception:
        return None


def generate(cfg, budget: Budget, channel: str, platform: str, transcript: str, signals: dict,
             source_url: str = "", seed: int = 0) -> Metadata:
    credit = cfg["publish"]["credit_template"].format(channel=channel, platform=platform.title())
    base = template_metadata(channel, platform, transcript, signals, credit, source_url, seed)
    mcfg = cfg["metadata"]
    if mcfg.get("use_llm"):
        m = llm_metadata(channel, platform, transcript, signals, budget, mcfg["llm_model"],
                         float(mcfg["llm_cost_per_call_inr"]))
        if m:
            # always keep the credit + base tags regardless of what the model wrote
            m.description = m.description.strip() + "\n\n" + base.description
            m.tags = list(dict.fromkeys(m.tags + base.tags))[:15]
            return m
    return base
