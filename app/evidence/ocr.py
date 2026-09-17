"""Video keyframe extraction so GPT-5.6 Terra can read payment videos as images.

ffmpeg is resolved in this order:
  1. an `ffmpeg` on PATH (Docker image: apt ffmpeg; servers: distro package)
  2. the static binary bundled by the `imageio-ffmpeg` pip package (development machines without brew/apt)
Duration is read from ffmpeg itself, so ffprobe is not required.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from functools import lru_cache
from pathlib import Path

from app.utils.logging import get_logger

log = get_logger("keyframes")
_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


@lru_cache
def ffmpeg_path() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


def ffmpeg_available() -> bool:
    return ffmpeg_path() is not None


async def video_duration(path: Path) -> float:
    """Seconds, parsed from `ffmpeg -i` (which prints stream info and exits non-zero without an output)."""
    exe = ffmpeg_path()
    if exe is None:
        return 0.0
    proc = await asyncio.create_subprocess_exec(
        exe, "-hide_banner", "-i", str(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, err = await proc.communicate()
    m = _DURATION.search(err.decode(errors="ignore"))
    if not m:
        return 0.0
    h, mnt, sec = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(sec)


async def extract_keyframes(video: Path, count: int = 4) -> list[Path]:
    exe = ffmpeg_path()
    if exe is None:
        log.warning("ffmpeg not available; skipping video keyframes", video=str(video))
        return []
    dur = await video_duration(video)
    if dur <= 0:
        log.warning("could not read video duration; skipping keyframes", video=str(video))
        return []
    out_dir = video.parent / f"{video.stem}_frames"
    out_dir.mkdir(exist_ok=True)
    frames: list[Path] = []
    # Sample evenly, and make sure the last frame is near the end, where confirmation screens usually appear.
    points = [dur * (i + 1) / (count + 1) for i in range(count)]
    if count:
        points[-1] = max(points[-1], dur - 1.0) if dur > 1.5 else points[-1]
    for i, t in enumerate(points):
        out = out_dir / f"frame_{i:02d}.jpg"
        proc = await asyncio.create_subprocess_exec(
            exe,
            "-y",
            "-hide_banner",
            "-v",
            "error",
            "-ss",
            f"{t:.2f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(out),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if out.exists() and out.stat().st_size > 0:
            frames.append(out)
    log.info("keyframes extracted", video=str(video), duration=round(dur, 2), frames=len(frames))
    return frames
