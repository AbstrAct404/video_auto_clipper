"""分镜捕捉（storyboard）：剪辑前先按镜头切分并逐镜采样，捕捉更多画面。

流程：ffmpeg scene 滤镜找切点 → 切点分段为镜头 → 每镜均匀采样多帧 →
单次批量解码 → 计算镜头级运动/亮度信号。输出供叙事目标匹配（narrative.py）
与混剪计划消费；只做像素级测量，不推断人物/语义（语义交 L2）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import Settings
from .media import MediaInfo, detect_scene_cuts, probe_media, sample_gray_frames_batch


@dataclass(frozen=True)
class Shot:
    index: int
    start_seconds: float
    end_seconds: float
    duration_seconds: float
    sampled_frames: int
    mean_motion_intensity: float
    peak_motion_intensity: float
    luminance_spike_ratio: float
    luminance_delta_max: float


def split_shots(duration_seconds: float, cuts: list[float]) -> list[tuple[float, float]]:
    """切点 → 镜头区间列表（过滤 ≤0.05s 的碎片镜头）。"""
    boundaries = [0.0, *[cut for cut in cuts if 0.0 < cut < duration_seconds], duration_seconds]
    shots: list[tuple[float, float]] = []
    for start, end in zip(boundaries, boundaries[1:]):
        if end - start > 0.05:
            shots.append((round(start, 3), round(end, 3)))
    return shots


def shot_sample_timestamps(
    start: float, end: float, frames_per_shot: int
) -> list[float]:
    """镜头内均匀布采样点（避开首尾 0.1s 的转场残留）。"""
    duration = end - start
    inset = min(0.1, duration * 0.2)
    usable_start, usable_end = start + inset, end - inset
    if usable_end <= usable_start:
        return [round((start + end) / 2, 3)]
    if frames_per_shot <= 1:
        return [round((usable_start + usable_end) / 2, 3)]
    step = (usable_end - usable_start) / (frames_per_shot - 1)
    return [round(usable_start + index * step, 3) for index in range(frames_per_shot)]


def _shot_signals(
    frames: list[np.ndarray], *, changed_pixel_threshold: float, spike_threshold: float
) -> tuple[float, float, float, float]:
    """相邻采样帧差分 → (mean/peak 运动强度, 亮度突变比例, 亮度最大突变)。"""
    if len(frames) < 2:
        return 0.0, 0.0, 0.0, 0.0
    ratios: list[float] = []
    for first, second in zip(frames, frames[1:]):
        diff = np.abs(first - second)
        ratios.append(float((diff >= changed_pixel_threshold).mean()))
    means = [float(frame.mean()) for frame in frames]
    lum_diffs = [abs(b - a) for a, b in zip(means, means[1:])]
    spikes = sum(1 for diff in lum_diffs if diff >= spike_threshold)
    return (
        sum(ratios) / len(ratios),
        max(ratios),
        spikes / len(lum_diffs),
        max(lum_diffs),
    )


def extract_storyboard(
    settings: Settings,
    video_path: str,
    *,
    frames_per_shot: int = 3,
    max_frames: int = 240,
    scene_threshold: float | None = None,
) -> tuple[MediaInfo, list[Shot]]:
    """全片分镜捕捉：切点分镜 + 逐镜多帧采样（单次批量解码）。

    帧预算不足时自动降低每镜采样数，保证覆盖全片所有镜头。
    """
    if frames_per_shot < 1:
        raise ValueError("frames_per_shot must be >= 1")
    info = probe_media(settings, video_path)
    cuts = detect_scene_cuts(
        settings, video_path, threshold=scene_threshold or settings.scene_cut_threshold
    )
    intervals = split_shots(info.duration_seconds, cuts)
    if not intervals:
        intervals = [(0.0, round(info.duration_seconds, 3))]
    # 帧预算自适应：优先覆盖全部镜头，其次保证每镜采样数
    per_shot = max(1, min(frames_per_shot, max_frames // len(intervals)))
    plan = [
        (index, start, end, shot_sample_timestamps(start, end, per_shot))
        for index, (start, end) in enumerate(intervals)
    ]
    all_timestamps = sorted({ts for _, _, _, stamps in plan for ts in stamps})
    frames = sample_gray_frames_batch(
        settings,
        video_path,
        all_timestamps,
        width=settings.motion_frame_width,
        source_size=(info.width, info.height) if info.width and info.height else None,
        max_frames=len(all_timestamps),
    )
    shots: list[Shot] = []
    for index, start, end, stamps in plan:
        ordered = [frames[ts] for ts in stamps if ts in frames]
        mean_motion, peak_motion, spike_ratio, delta_max = _shot_signals(
            ordered,
            changed_pixel_threshold=settings.changed_pixel_threshold,
            spike_threshold=settings.luminance_spike_threshold,
        )
        shots.append(
            Shot(
                index=index,
                start_seconds=start,
                end_seconds=end,
                duration_seconds=round(end - start, 3),
                sampled_frames=len(ordered),
                mean_motion_intensity=round(mean_motion, 4),
                peak_motion_intensity=round(peak_motion, 4),
                luminance_spike_ratio=round(spike_ratio, 4),
                luminance_delta_max=round(delta_max, 4),
            )
        )
    return info, shots
