"""Vertical 9:16 editing with FFmpeg: blurred full-frame background, the
original 16:9 frame centred (slightly zoomed), burned-in captions and a
creator credit line."""
from __future__ import annotations

from pathlib import Path

from .ffmpeg_utils import FFMPEG, has_audio, run

CAPTION_STYLE = ("FontName=DejaVu Sans,FontSize=13,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                 "BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=60")


def _esc_filter_path(p: Path) -> str:
    # escape for use inside an ffmpeg filtergraph option value
    return str(p.resolve()).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _esc_text(s: str) -> str:
    return s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’").replace("%", "\\%")


def make_vertical(src: Path, dst: Path, srt: Path | None = None, credit: str | None = None,
                  width: int = 1080, height: int = 1920, crf: int = 23, preset: str = "veryfast",
                  max_duration: float = 59, zoom: float = 1.25, threads: int = 2) -> Path:
    fg_w = int(width * zoom) // 2 * 2
    chain = [
        f"[0:v]split=2[bgsrc][fgsrc]",
        f"[bgsrc]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
        f"boxblur=20:2,eq=brightness=-0.08[bg]",
        f"[fgsrc]scale={fg_w}:-2,crop=min(iw\\,{width}):ih[fg]",
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2-80[base]",
    ]
    last = "base"
    if credit:
        chain.append(f"[{last}]drawtext=font='DejaVu Sans':text='{_esc_text(credit)}':fontcolor=white@0.9:"
                     f"fontsize=38:box=1:boxcolor=black@0.45:boxborderw=14:x=(w-text_w)/2:y=170[cred]")
        last = "cred"
    if srt and Path(srt).exists() and Path(srt).stat().st_size > 0:
        chain.append(f"[{last}]subtitles=filename='{_esc_filter_path(Path(srt))}':force_style='{CAPTION_STYLE}'[subs]")
        last = "subs"
    chain.append(f"[{last}]format=yuv420p[v]")
    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-t", str(max_duration),
            "-filter_complex", ";".join(chain), "-map", "[v]"]
    if has_audio(src):
        args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "160k", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
    args += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-r", "30", "-threads", str(threads),
             "-movflags", "+faststart", str(dst)]
    run(args, timeout=900)
    return Path(dst)
