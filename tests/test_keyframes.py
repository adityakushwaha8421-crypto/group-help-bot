"""Real ffmpeg: generate an MP4 and extract keyframes from it."""

import subprocess
from pathlib import Path

import pytest

from app.evidence.ocr import extract_keyframes, ffmpeg_available, ffmpeg_path, video_duration


@pytest.fixture
def sample_mp4(tmp_path) -> Path:
    if not ffmpeg_available():
        pytest.skip("ffmpeg not available")
    out = tmp_path / "payment.mp4"
    subprocess.run(
        [
            ffmpeg_path(),
            "-y",
            "-hide_banner",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=10",
            "-t",
            "6",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ],
        check=True,
    )
    assert out.stat().st_size > 0
    return out


async def test_keyframes_from_real_mp4(sample_mp4):
    dur = await video_duration(sample_mp4)
    assert 5.5 <= dur <= 6.5
    frames = await extract_keyframes(sample_mp4, 4)
    assert len(frames) == 4
    assert all(f.suffix == ".jpg" and f.stat().st_size > 500 for f in frames)
    assert frames[-1].read_bytes()[:2] == b"\xff\xd8"  # JPEG magic


async def test_missing_video_yields_no_frames(tmp_path):
    assert await extract_keyframes(tmp_path / "nope.mp4", 4) == []
