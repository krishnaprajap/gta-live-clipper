"""Mix a speech recording into an existing test video so captions can be
verified end to end.

    python scripts/add_speech.py --video data/test/stream.mp4 --speech speech.wav --at 41 --out data/test/stream_speech.mp4

--at: second where speech starts. The default test video's exciting moment is
at 55 s and clips start ~17 s before it, so 41 s puts the speech inside the clip
and before the loud part."""
import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from clipper.ffmpeg_utils import FFMPEG  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--video", required=True)
ap.add_argument("--speech", required=True)
ap.add_argument("--at", type=float, default=41.0)
ap.add_argument("--out", required=True)
a = ap.parse_args()
ms = int(a.at * 1000)
subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", a.video, "-i", a.speech,
                "-filter_complex", f"[1:a]aresample=48000,adelay={ms}|{ms},volume=2.5[s];"
                                   f"[0:a][s]amix=inputs=2:normalize=0:duration=first[a]",
                "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", a.out], check=True)
print(a.out)
