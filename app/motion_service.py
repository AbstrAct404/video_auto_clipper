"""运动分析服务：窗口采样 + absdiff + 时序变化分类。

语义与阈值对齐 Framework 的 TemporalChangeAnalyzer（stable/intermittent/
continuous/abrupt），但改为按 HTTP 请求驱动；只报告像素级测量，不推断
主体身份或意图。
"""

from __future__ import annotations

import numpy as np

from .config import Settings
from .media import MediaInfo, sample_gray_frames
from .models import (
    MotionAnalysisRequest,
    MotionAnalysisResponse,
    MotionWindowRequest,
    MotionWindowResult,
    NormalizedROI,
    PixelDifference,
    TemporalSummary,
)

# 与 Framework PROFILE_SETTINGS 同口径
PROFILE_SETTINGS: dict[str, tuple[float, float]] = {
    "burst_1s_3fps": (1.0, 3.0),
    "window_3s_1fps": (3.0, 1.0),
}


def build_window_timestamps(
    window: MotionWindowRequest, *, duration_seconds: float
) -> list[float]:
    duration, samples_per_second = PROFILE_SETTINGS[window.profile]
    sample_count = max(2, round(duration * samples_per_second))
    interval = 1.0 / samples_per_second
    timestamps = [
        round(window.start_seconds + index * interval, 6)
        for index in range(sample_count)
    ]
    return [ts for ts in timestamps if ts < duration_seconds]


def _crop(frame: np.ndarray, roi: NormalizedROI) -> np.ndarray:
    height, width = frame.shape
    x0, x1 = round(roi.x_min * width), round(roi.x_max * width)
    y0, y1 = round(roi.y_min * height), round(roi.y_max * height)
    cropped = frame[y0:y1, x0:x1]
    if cropped.size == 0:
        raise ValueError("motion ROI resolves to no pixels")
    return cropped


def compare_pair(
    first: np.ndarray,
    second: np.ndarray,
    *,
    roi: NormalizedROI | None,
    changed_pixel_threshold: float,
) -> tuple[float, float]:
    """返回 (mean_absolute_difference, changed_pixel_ratio)，像素值域 0~1。"""
    if first.shape != second.shape:
        raise ValueError("motion frame dimensions must match")
    a, b = (
        (_crop(first, roi), _crop(second, roi)) if roi is not None else (first, second)
    )
    diff = np.abs(a - b)
    return float(diff.mean()), float((diff >= changed_pixel_threshold).mean())


def classify_temporal(
    mean_diffs: list[float],
    changed_ratios: list[float],
    *,
    settings: Settings,
) -> TemporalSummary:
    active = [ratio >= settings.active_changed_pixel_ratio for ratio in changed_ratios]
    abrupt = any(
        ratio >= settings.abrupt_changed_pixel_ratio
        and mean >= settings.abrupt_mean_absolute_difference
        for mean, ratio in zip(mean_diffs, changed_ratios)
    )
    if abrupt:
        pattern = "abrupt"
    elif all(active):
        pattern = "continuous"
    elif any(active):
        pattern = "intermittent"
    else:
        pattern = "stable"
    return TemporalSummary(
        pattern=pattern,
        continuous_change=pattern == "continuous",
        active_pair_count=sum(active),
        pair_count=len(mean_diffs),
        mean_absolute_difference=sum(mean_diffs) / len(mean_diffs),
        max_absolute_difference=max(mean_diffs),
        mean_changed_pixel_ratio=sum(changed_ratios) / len(changed_ratios),
        peak_changed_pixel_ratio=max(changed_ratios),
    )


def analyze_window(
    settings: Settings,
    video_path: str,
    window: MotionWindowRequest,
    *,
    info: MediaInfo,
    frame_width: int,
    frames: dict[float, np.ndarray] | None = None,
) -> MotionWindowResult:
    """窗口运动分析。frames 可由调用方批量抽好传入（避免逐窗 seek）。"""
    profile_duration, _ = PROFILE_SETTINGS[window.profile]
    timestamps = build_window_timestamps(window, duration_seconds=info.duration_seconds)
    if len(timestamps) < 2:
        status = (
            "outside_duration"
            if window.start_seconds >= info.duration_seconds
            else "insufficient_data"
        )
        return MotionWindowResult(
            start_seconds=window.start_seconds,
            duration_seconds=profile_duration,
            profile=window.profile,
            status=status,
            notes=["window does not contain at least two in-duration samples"],
        )
    if frames is None:
        frames = sample_gray_frames(settings, video_path, timestamps, width=frame_width)
    # Keep timestamps and frames coupled.  If an extractor returns a partial
    # result, filtering only the frame list would otherwise pair a frame with
    # the wrong timestamp in the zip below.
    samples = [(ts, frames[ts]) for ts in timestamps if ts in frames]
    if len(samples) < 2:
        return MotionWindowResult(
            start_seconds=window.start_seconds,
            duration_seconds=profile_duration,
            profile=window.profile,
            status="insufficient_data",
            notes=["fewer than two sampled frames available"],
        )
    mean_diffs: list[float] = []
    changed_ratios: list[float] = []
    differences: list[PixelDifference] = []
    for (first_ts, first), (second_ts, second) in zip(samples, samples[1:]):
        mean, ratio = compare_pair(
            first,
            second,
            roi=window.roi,
            changed_pixel_threshold=settings.changed_pixel_threshold,
        )
        mean_diffs.append(mean)
        changed_ratios.append(ratio)
        differences.append(
            PixelDifference(
                first_timestamp_seconds=first_ts,
                second_timestamp_seconds=second_ts,
                mean_absolute_difference=mean,
                changed_pixel_ratio=ratio,
            )
        )
    return MotionWindowResult(
        start_seconds=window.start_seconds,
        duration_seconds=profile_duration,
        profile=window.profile,
        status="completed",
        temporal_summary=classify_temporal(mean_diffs, changed_ratios, settings=settings),
        pixel_differences=differences,
        notes=["absdiff measures pixel change only; no subject intent inference"],
    )


def analyze_motion(
    settings: Settings, request: MotionAnalysisRequest, *, info: MediaInfo
) -> MotionAnalysisResponse:
    frame_width = request.frame_width or settings.motion_frame_width
    results = [
        analyze_window(
            settings, request.video_path, window, info=info, frame_width=frame_width
        )
        for window in request.windows
    ]
    return MotionAnalysisResponse(
        video_path=request.video_path,
        duration_seconds=info.duration_seconds,
        frame_width=frame_width,
        windows=results,
    )
