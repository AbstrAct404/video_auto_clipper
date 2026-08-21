"""L3 剪辑计划：从带分数的候选窗口挑选拼接片段（确定性贪心，一期骨架）。

输入是运动分析窗口（起点 + 强度分数），输出 15s 成片的片段计划。
本模块只生成可审计、可回放的时间轴；FFmpeg 执行由 clip_executor.py 完成，
两者保持分离以便审核与重试。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScoredWindow:
    start_seconds: float
    duration_seconds: float
    score: float  # 越高越优先入片（如窗口平均运动强度）


def select_segments(
    windows: list[ScoredWindow],
    *,
    target_duration_seconds: float = 15.0,
    min_segment_seconds: float = 1.0,
) -> list[dict[str, float]]:
    """按「分数优先 + 新增有效时长」贪心挑窗。

    重叠窗口只按新增的独立时间计入预算，避免旧实现先重复累计窗口时长、
    再合并而导致有效成片不足。返回按时间排序的片段；合并片段的分数取
    其中最高分窗口，以保留最强候选信号。窗口总时长不足时返回最大集合。
    """
    if target_duration_seconds <= 0:
        raise ValueError("target_duration_seconds must be positive")
    if min_segment_seconds < 0:
        raise ValueError("min_segment_seconds must not be negative")
    for window in windows:
        if window.start_seconds < 0 or window.duration_seconds <= 0:
            raise ValueError("windows require non-negative starts and positive durations")

    ordered = sorted(windows, key=lambda w: (-w.score, w.start_seconds, w.duration_seconds))
    picked: list[ScoredWindow] = []

    def union_duration(items: list[ScoredWindow]) -> float:
        intervals = sorted(
            (item.start_seconds, item.start_seconds + item.duration_seconds) for item in items
        )
        total = 0.0
        start = end = None
        for current_start, current_end in intervals:
            if start is None:
                start, end = current_start, current_end
            elif current_start <= end:
                end = max(end, current_end)
            else:
                total += end - start
                start, end = current_start, current_end
        return total + (end - start if start is not None else 0.0)

    covered_duration = 0.0
    for window in ordered:
        if window.duration_seconds < min_segment_seconds:
            continue
        if covered_duration >= target_duration_seconds:
            break
        new_covered_duration = union_duration([*picked, window])
        # 完全落在已选片段内的窗口不应占用目标时长预算。
        if new_covered_duration <= covered_duration:
            continue
        picked.append(window)
        covered_duration = new_covered_duration

    # 按时间排序后合并相邻/重叠窗口
    timeline: list[dict[str, float]] = []
    for window in sorted(picked, key=lambda w: w.start_seconds):
        end = window.start_seconds + window.duration_seconds
        if timeline and window.start_seconds <= timeline[-1]["end_seconds"]:
            last = timeline[-1]
            last["end_seconds"] = max(last["end_seconds"], end)
            last["score"] = max(last["score"], window.score)
        else:
            timeline.append(
                {"start_seconds": window.start_seconds, "end_seconds": end, "score": window.score}
            )
    # 裁掉超出目标时长的尾部（保持片段起点不变，截断最后一个片段）；
    # 截断后剩余不足 min_segment_seconds 的尾段直接丢弃，避免在中长片段后
    # 挂一个 <1s 的孤立短镜（节奏守卫与 narrative 层保持一致）。
    budget = target_duration_seconds
    clipped: list[dict[str, float]] = []
    for segment in timeline:
        length = segment["end_seconds"] - segment["start_seconds"]
        if budget <= 0:
            break
        if length > budget:
            if budget < min_segment_seconds:
                break
            segment = {**segment, "end_seconds": round(segment["start_seconds"] + budget, 3)}
            length = budget
        clipped.append(segment)
        budget -= length
    return clipped


def plan_status() -> dict[str, str]:
    """L3 当前能力状态。"""
    return {
        "segment_planner": "implemented",
        "segment_executor": "implemented",
        "transition_audio_blend": "not_implemented",
    }
