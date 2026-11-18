"""Generate a synthetic, copyright-free 'live stream' recording plus a chat
replay for offline end-to-end tests. Quiet gameplay-like footage with an
'exciting moment' at ~EVENT seconds: loud audio, rapid scene cuts, chat burst."""
import argparse
import json
import random
import subprocess
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--out", default="data/test/stream.mp4")
ap.add_argument("--duration", type=int, default=90)
ap.add_argument("--event", type=int, default=55)
a = ap.parse_args()
out = Path(a.out)
out.parent.mkdir(parents=True, exist_ok=True)
D, E = a.duration, a.event
# video: calm testsrc2 except a 6 s burst of flashing colour cuts at the event
video = (f"testsrc2=size=1280x720:rate=30:duration={D}[calm];"
         f"color=c=red:size=1280x720:rate=30:duration={D},hue=H=t*20:s=4[flash];"
         f"[calm][flash]overlay=enable='between(t,{E},{E+6})*lt(mod(t,0.5),0.25)'[v]")
# audio: quiet hum, loud noise + tone during the event
audio = (f"anoisesrc=d={D}:c=pink:a=0.02[bed];sine=f=220:d={D},volume=0.05[hum];"
         f"anoisesrc=d={D}:c=white:a=0.6,volume='if(between(t,{E},{E+6}),1,0)':eval=frame[boom];"
         f"[bed][hum][boom]amix=inputs=3:normalize=0[a]")
subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-filter_complex", video + ";" + audio, "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "veryfast", "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
                "-c:a", "aac", "-shortest", str(out)], check=True)
rnd = random.Random(1)
chat = []
for t in range(D):
    n = rnd.randint(0, 2)
    if E <= t <= E + 8:
        n = rnd.randint(12, 20)
    for _ in range(n):
        txt = rnd.choice(["KEKW", "POGGERS", "no way", "CLIP IT", "LMAO 😂", "WHAT"]) if E <= t <= E + 8 else \
            rnd.choice(["hi", "what mission is this", "nice car", "lol", "gg"])
        chat.append({"t": t + rnd.random(), "text": txt})
chat_path = out.with_name("chat.jsonl")
chat_path.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in chat), encoding="utf-8")
print(out, chat_path)
