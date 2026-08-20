"""共享 fixture：用 ffmpeg 生成确定性小视频（4s，含一次场景切换 + 音频）。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def small_video(tmp_path_factory) -> Path:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available")
    out_dir = tmp_path_factory.mktemp("videos")
    video = out_dir / "fixture.mp4"
    # 前 2s testsrc2（持续变化的图案 → 高运动），后 2s smptebars（静止 → 低运动），
    # 中间形成一次硬切；叠加正弦音轨供 volumedetect。
    cmd = [
        "ffmpeg",
        "-v", "error",
        "-y",
        "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=10:duration=2",
        "-f", "lavfi", "-i", "smptebars=size=128x128:rate=10:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-map", "2:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest",
        str(video),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        pytest.skip(f"ffmpeg fixture generation failed: {result.stderr[-200:]}")
    return video
