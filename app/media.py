"""ffmpeg/ffprobe 媒体工具：探测、按时间戳抽帧（灰度 rawvideo）、场景切点、音量。

全部为确定性本地调用，零 API 成本。抽帧统一降宽 + 灰度，控制 CPU 与内存。
"""

from __future__ import annotations

import re
import subprocess
import base64
from dataclasses import dataclass

import numpy as np

from .config import Settings

_SHOWINFO_PTS = re.compile(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)")
_MEAN_VOLUME = re.compile(r"mean_volume:\s*(-?[0-9]+(?:\.[0-9]+)?)\s*dB")


class MediaError(RuntimeError):
    """媒体探测/抽帧失败（文件不存在、ffmpeg 失败、解析失败）。"""


@dataclass(frozen=True)
class MediaInfo:
    duration_seconds: float
    width: int | None
    height: int | None
    fps: float | None
    has_audio: bool


def probe_media(settings: Settings, video_path: str) -> MediaInfo:
    cmd = [
        settings.ffprobe_bin,
        "-v", "error",
        "-print_format", "default=noprint_wrappers=1",
        "-show_entries",
        "format=duration:stream=codec_type,width,height,avg_frame_rate",
        video_path,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, check=True
        )
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        raise MediaError(f"ffprobe failed for {video_path}: {exc}") from exc

    duration: float | None = None
    width = height = None
    fps: float | None = None
    has_audio = False
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        value = value.strip()
        if key == "duration" and duration is None:
            duration = float(value)
        elif key == "codec_type" and value == "audio":
            has_audio = True
        elif key == "width" and width is None:
            width = int(value)
        elif key == "height" and height is None:
            height = int(value)
        elif key == "avg_frame_rate" and fps is None and "/" in value:
            num, _, den = value.partition("/")
            if float(den or 0) > 0:
                fps = float(num) / float(den)
    if duration is None:
        raise MediaError(f"cannot parse duration for {video_path}")
    return MediaInfo(duration, width, height, fps, has_audio)


def sample_gray_frames(
    settings: Settings,
    video_path: str,
    timestamps_seconds: list[float],
    *,
    width: int,
) -> dict[float, np.ndarray]:
    """逐时间戳抽取灰度帧（等宽降采样），返回 {timestamp: HxW float32 数组(0~1)}。

    适合少量任意时间戳（视觉分析）。大批量等间隔抽帧请用
    sample_gray_frames_batch（单次解码，成本低一个量级）。
    """
    frames: dict[float, np.ndarray] = {}
    for ts in timestamps_seconds:
        cmd = [
            settings.ffmpeg_bin,
            "-v", "error",
            "-ss", f"{ts:.3f}",
            "-i", video_path,
            "-frames:v", "1",
            "-vf", f"scale={width}:-2",
            "-f", "rawvideo",
            "-pix_fmt", "gray",
            "-",
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=settings.ffmpeg_timeout_seconds,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise MediaError(
                f"frame extraction failed at {ts:.3f}s: "
                f"{exc.stderr.decode(errors='replace')[-300:]}"
            ) from exc
        except subprocess.SubprocessError as exc:
            raise MediaError(f"frame extraction timed out at {ts:.3f}s") from exc
        raw = result.stdout
        if len(raw) < width:
            raise MediaError(f"no decodable frame at {ts:.3f}s")
        height = len(raw) // width
        frame = np.frombuffer(raw[: height * width], dtype=np.uint8)
        frames[ts] = frame.reshape(height, width).astype("float32") / 255.0
    return frames


def extract_jpeg_data_url(
    settings: Settings, video_path: str, *, timestamp_seconds: float, width: int
) -> str:
    """抽取一帧 JPEG 并编码为 data URL，供支持多模态的 L2 直接消费。"""
    cmd = [
        settings.ffmpeg_bin,
        "-v",
        "error",
        "-ss",
        f"{timestamp_seconds:.3f}",
        "-i",
        video_path,
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:-2",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=settings.ffmpeg_timeout_seconds, check=True
        )
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        raise MediaError(f"L2 frame extraction failed at {timestamp_seconds:.3f}s: {exc}") from exc
    if not result.stdout:
        raise MediaError(f"L2 frame extraction produced no image at {timestamp_seconds:.3f}s")
    encoded = base64.b64encode(result.stdout).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def sample_gray_frames_batch(
    settings: Settings,
    video_path: str,
    timestamps_seconds: list[float],
    *,
    width: int,
    source_size: tuple[int, int] | None,
    max_frames: int = 240,
) -> dict[float, np.ndarray]:
    """单次解码批量抽灰度帧：fps 重采样 + -t 限长，避免逐帧 seek 启动开销。

    采样率自适应：max_frames 预算内尽量取 1fps，长视频自动降频；
    时间戳映射误差 ≤ 1/(2*rate) 秒。未知分辨率时退化为逐帧抽取。
    """
    if not timestamps_seconds:
        return {}
    targets = sorted(set(timestamps_seconds))
    if source_size is None:
        return sample_gray_frames(settings, video_path, targets, width=width)
    src_w, src_h = source_size
    if width < 2 or src_w <= 0 or src_h <= 0:
        raise MediaError("invalid source or output frame dimensions")
    height = max(2, int(round(width * src_h / src_w)))
    height += height % 2  # 对齐 scale=-2 的偶数约束
    span = max(targets) + 1.0
    rate = min(1.0, max_frames / span)
    cmd = [
        settings.ffmpeg_bin,
        "-v", "error",
        "-i", video_path,
        "-t", f"{span:.3f}",
        "-vf", f"fps={rate:.6f},scale={width}:-2",
        "-f", "rawvideo",
        "-pix_fmt", "gray",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=settings.ffmpeg_timeout_seconds,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise MediaError(
            f"batch frame extraction failed: {exc.stderr.decode(errors='replace')[-300:]}"
        ) from exc
    except subprocess.SubprocessError as exc:
        raise MediaError("batch frame extraction timed out") from exc
    frame_size = width * height
    if len(result.stdout) < frame_size:
        raise MediaError("batch extraction produced no decodable frame")
    count = len(result.stdout) // frame_size
    if count < 1:
        raise MediaError("batch extraction produced incomplete frames")
    frames: dict[float, np.ndarray] = {}
    for ts in targets:
        index = min(int(round(ts * rate)), count - 1)
        chunk = result.stdout[index * frame_size : (index + 1) * frame_size]
        frame = np.frombuffer(chunk, dtype=np.uint8)
        frames[ts] = frame.reshape(height, width).astype("float32") / 255.0
    return frames


def detect_scene_cuts(
    settings: Settings, video_path: str, *, threshold: float
) -> list[float]:
    """FFmpeg select 滤镜场景切点（秒），按时间升序。"""
    cmd = [
        settings.ffmpeg_bin,
        "-v", "info",
        "-i", video_path,
        "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-f", "null",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=settings.ffmpeg_timeout_seconds,
        )
    except subprocess.SubprocessError as exc:
        raise MediaError("scene detection timed out") from exc
    if result.returncode != 0:
        raise MediaError(
            f"scene detection failed: {result.stderr[-300:]}"
        )
    cuts = [float(match) for match in _SHOWINFO_PTS.findall(result.stderr)]
    return sorted(cuts)


def detect_audio_mean_volume(settings: Settings, video_path: str) -> float | None:
    """volumedetect 平均音量（dB）；无音轨返回 None。"""
    cmd = [
        settings.ffmpeg_bin,
        "-v", "info",
        "-i", video_path,
        "-af", "volumedetect",
        "-f", "null",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=settings.ffmpeg_timeout_seconds,
        )
    except subprocess.SubprocessError as exc:
        raise MediaError("audio volumedetect timed out") from exc
    if result.returncode != 0:
        raise MediaError(f"volumedetect failed: {result.stderr[-300:]}")
    match = _MEAN_VOLUME.search(result.stderr)
    return float(match.group(1)) if match else None
