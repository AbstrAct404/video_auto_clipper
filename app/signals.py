"""L0 信号层：本地零成本确定性信号计算（一期实现）。

信号清单与设计依据见 docs/signal-layer-design.md。全部基于 ffmpeg/numpy，
零 API 费用；输出统一 SignalValues 契约，供风格画像规则库消费。
"""

import numpy as np

from .config import Settings
from .media import (
    MediaError,
    detect_audio_mean_volume,
    detect_scene_cuts,
    probe_media,
    sample_gray_frames_batch,
)
from .models import (
    CandidateWindow,
    MotionWindowRequest,
    SignalsRequest,
    SignalsResponse,
    SignalValues,
)
from .motion_service import analyze_window, build_window_timestamps


def _luminance_spike_ratio_from_frames(
    settings: Settings, frames: dict[float, np.ndarray], timestamps: list[float]
) -> float:
    """相邻采样帧平均亮度差超阈比例（特效/闪变粗信号）。"""
    ordered = [frames[ts] for ts in timestamps if ts in frames]
    if len(ordered) < 2:
        return 0.0
    means = [float(frame.mean()) for frame in ordered]
    diffs = [abs(b - a) for a, b in zip(means, means[1:])]
    spikes = sum(1 for diff in diffs if diff >= settings.luminance_spike_threshold)
    return spikes / len(diffs)


def compute_signals(
    settings: Settings, request: SignalsRequest
) -> SignalsResponse:
    video_path = request.video_path
    info = probe_media(settings, video_path)
    notes: list[str] = []

    # 1) 切镜率：场景切点计数 / 分钟
    scene_threshold = request.scene_threshold or settings.scene_cut_threshold
    cuts = detect_scene_cuts(settings, video_path, threshold=scene_threshold)
    minutes = max(info.duration_seconds / 60.0, 1e-6)
    shot_cut_rate = len(cuts) / minutes

    # 2) 运动强度：全片均匀布窗（window_3s_1fps），聚合 mean_changed_pixel_ratio
    window_count = request.motion_window_count or settings.signal_motion_window_count
    # 窗口起点均匀铺开，并保证窗口完整落在时长内
    max_start = max(info.duration_seconds - 3.0, 0.0)
    starts = (
        [0.0]
        if window_count == 1 or max_start == 0.0
        else [round(max_start * index / (window_count - 1), 3) for index in range(window_count)]
    )
    windows = [
        MotionWindowRequest(start_seconds=start, profile="window_3s_1fps")
        for start in starts
    ]

    # 3) 亮度突变采样点：全片等间隔（与运动窗合并后一次性批量解码）
    fps = settings.signal_luminance_fps
    lum_count = min(settings.signal_luminance_max_frames, max(2, int(info.duration_seconds * fps)))
    step = info.duration_seconds / lum_count
    luminance_timestamps = [
        ts for ts in (round(index * step + step / 2, 3) for index in range(lum_count))
        if ts < info.duration_seconds
    ]

    all_timestamps = sorted(
        {
            ts
            for window in windows
            for ts in build_window_timestamps(window, duration_seconds=info.duration_seconds)
        }
        | set(luminance_timestamps)
    )
    frames = sample_gray_frames_batch(
        settings,
        video_path,
        all_timestamps,
        width=settings.motion_frame_width,
        source_size=(info.width, info.height),
        max_frames=len(all_timestamps),
    )

    intensities: list[float] = []
    candidates: list[CandidateWindow] = []
    continuous_windows = 0
    completed_windows = 0
    for window in windows:
        result = analyze_window(
            settings,
            video_path,
            window,
            info=info,
            frame_width=settings.motion_frame_width,
            frames=frames,
        )
        if result.status != "completed" or result.temporal_summary is None:
            notes.append(f"motion window at {window.start_seconds}s skipped: {result.status}")
            continue
        completed_windows += 1
        intensities.append(result.temporal_summary.mean_changed_pixel_ratio)
        if result.temporal_summary.continuous_change:
            continuous_windows += 1
        window_timestamps = build_window_timestamps(
            window, duration_seconds=info.duration_seconds
        )
        window_spike_ratio = _luminance_spike_ratio_from_frames(
            settings, frames, window_timestamps
        )
        # 可解释的初始排序模型：运动为主，连续性/亮度突变为补充。
        # 后续以人工选片接受率标定这些权重，而不是把它们写进规则卡。
        motion_score = min(1.0, result.temporal_summary.mean_changed_pixel_ratio / 0.5)
        continuity_score = 1.0 if result.temporal_summary.continuous_change else 0.0
        luminance_score = min(1.0, window_spike_ratio / max(settings.luminance_spike_threshold, 1e-6))
        score = 0.65 * motion_score + 0.20 * continuity_score + 0.15 * luminance_score
        candidates.append(
            CandidateWindow(
                start_seconds=window.start_seconds,
                duration_seconds=min(3.0, info.duration_seconds - window.start_seconds),
                score=round(score, 4),
                motion_intensity=round(result.temporal_summary.mean_changed_pixel_ratio, 4),
                peak_motion_intensity=round(result.temporal_summary.peak_changed_pixel_ratio, 4),
                temporal_pattern=result.temporal_summary.pattern,
                luminance_spike_ratio=round(window_spike_ratio, 4),
                score_components={
                    "motion": round(motion_score, 4),
                    "continuity": continuity_score,
                    "luminance": round(luminance_score, 4),
                },
            )
        )
    if not intensities:
        raise MediaError("no motion window produced a measurement")

    # 4) 音频能量：volumedetect 平均音量（dB）
    audio_db = None
    if info.has_audio:
        audio_db = detect_audio_mean_volume(settings, video_path)
    else:
        notes.append("no audio stream; audio_mean_volume_db is null")

    # 5) 亮度突变比例（特效/闪变粗信号）
    spike_ratio = _luminance_spike_ratio_from_frames(
        settings, frames, luminance_timestamps
    )

    return SignalsResponse(
        video_path=video_path,
        signals=SignalValues(
            shot_cut_rate_per_min=round(shot_cut_rate, 3),
            scene_count=len(cuts),
            mean_motion_intensity=round(sum(intensities) / len(intensities), 4),
            peak_motion_intensity=round(max(intensities), 4),
            continuous_motion_window_ratio=(
                round(continuous_windows / completed_windows, 3)
                if completed_windows
                else 0.0
            ),
            audio_mean_volume_db=audio_db,
            luminance_spike_ratio=round(spike_ratio, 4),
            duration_seconds=info.duration_seconds,
            width=info.width,
            height=info.height,
            fps=round(info.fps, 3) if info.fps else None,
        ),
        candidates=sorted(candidates, key=lambda item: (-item.score, item.start_seconds)),
        notes=notes,
    )
