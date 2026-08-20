"""L3 成片执行器：按剪辑计划切段并通过 FFmpeg concat 输出 MP4。"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .config import Settings
from .media import MediaError
from .models import ClipSegment


def render_segments(
    settings: Settings,
    *,
    video_path: str,
    segments: list[ClipSegment],
    output_path: str,
    overwrite: bool,
) -> None:
    """重编码每段以统一时间基，再使用 concat demuxer 无缝拼接。"""
    target = Path(output_path)
    if target.exists() and not overwrite:
        raise MediaError(f"output already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="smartclip-render-") as workdir:
        work = Path(workdir)
        parts: list[Path] = []
        for index, segment in enumerate(segments):
            duration = segment.end_seconds - segment.start_seconds
            if duration <= 0:
                raise MediaError("clip segment end_seconds must exceed start_seconds")
            part = work / f"part-{index:03d}.mp4"
            cmd = [
                settings.ffmpeg_bin,
                "-y",
                "-ss",
                f"{segment.start_seconds:.3f}",
                "-t",
                f"{duration:.3f}",
                "-i",
                video_path,
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-movflags",
                "+faststart",
                str(part),
            ]
            _run(settings, cmd, f"segment {index}")
            parts.append(part)

        concat_file = work / "concat.txt"
        concat_file.write_text(
            "".join(f"file '{part.as_posix()}'\n" for part in parts), encoding="utf-8"
        )
        cmd = [
            settings.ffmpeg_bin,
            "-y" if overwrite else "-n",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(target),
        ]
        _run(settings, cmd, "concat")


def _run(settings: Settings, cmd: list[str], stage: str) -> None:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=settings.ffmpeg_timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaError(f"clip render {stage} failed: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode(errors="replace")[-500:]
        raise MediaError(f"clip render {stage} failed: {detail}")
