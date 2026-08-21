"""L3 剪辑计划测试：贪心挑窗 + 合并 + 预算裁剪。"""

from __future__ import annotations

import pytest

from app.clip_planner import ScoredWindow, plan_status, select_segments


def test_picks_highest_score_first():
    windows = [
        ScoredWindow(start_seconds=0.0, duration_seconds=5.0, score=0.3),
        ScoredWindow(start_seconds=20.0, duration_seconds=5.0, score=0.9),
        ScoredWindow(start_seconds=40.0, duration_seconds=5.0, score=0.6),
    ]
    segments = select_segments(windows, target_duration_seconds=10.0)
    starts = [segment["start_seconds"] for segment in segments]
    # 高分窗口（20s、40s）入选，低分（0s）被挤掉
    assert starts == [20.0, 40.0]
    total = sum(s["end_seconds"] - s["start_seconds"] for s in segments)
    assert total == pytest.approx(10.0)


def test_adjacent_windows_merged():
    windows = [
        ScoredWindow(start_seconds=10.0, duration_seconds=5.0, score=0.8),
        ScoredWindow(start_seconds=15.0, duration_seconds=5.0, score=0.7),
    ]
    segments = select_segments(windows, target_duration_seconds=15.0)
    assert len(segments) == 1
    assert segments[0]["start_seconds"] == 10.0
    assert segments[0]["end_seconds"] == 20.0


def test_overlapping_windows_use_unique_duration_budget():
    """重叠候选不应在挑选阶段重复消耗时长预算。"""
    windows = [
        ScoredWindow(start_seconds=0.0, duration_seconds=10.0, score=0.9),
        ScoredWindow(start_seconds=2.0, duration_seconds=8.0, score=0.8),
        ScoredWindow(start_seconds=20.0, duration_seconds=5.0, score=0.7),
    ]
    segments = select_segments(windows, target_duration_seconds=15.0)
    assert [(item["start_seconds"], item["end_seconds"]) for item in segments] == [
        (0.0, 10.0),
        (20.0, 25.0),
    ]


def test_budget_clips_tail():
    windows = [
        ScoredWindow(start_seconds=0.0, duration_seconds=10.0, score=0.9),
        ScoredWindow(start_seconds=30.0, duration_seconds=10.0, score=0.8),
    ]
    segments = select_segments(windows, target_duration_seconds=15.0)
    total = sum(s["end_seconds"] - s["start_seconds"] for s in segments)
    assert total == pytest.approx(15.0)
    # 第二个片段被截断到 5s
    assert segments[-1]["end_seconds"] == pytest.approx(35.0)


def test_tail_clip_drops_short_remainder():
    # 10s 片段占满预算后仅剩 0.5s，不足以构成独立片段 → 丢弃而非产生 <1s 短镜
    windows = [
        ScoredWindow(start_seconds=0.0, duration_seconds=10.0, score=0.9),
        ScoredWindow(start_seconds=30.0, duration_seconds=10.0, score=0.8),
    ]
    segments = select_segments(windows, target_duration_seconds=10.5)
    assert len(segments) == 1
    assert segments[0]["end_seconds"] == pytest.approx(10.0)


def test_short_windows_filtered_by_min_segment():
    windows = [
        ScoredWindow(start_seconds=0.0, duration_seconds=0.5, score=0.99),
        ScoredWindow(start_seconds=5.0, duration_seconds=3.0, score=0.4),
    ]
    segments = select_segments(
        windows, target_duration_seconds=15.0, min_segment_seconds=1.0
    )
    assert len(segments) == 1
    assert segments[0]["start_seconds"] == 5.0


def test_insufficient_windows_returns_partial():
    windows = [ScoredWindow(start_seconds=0.0, duration_seconds=3.0, score=0.5)]
    segments = select_segments(windows, target_duration_seconds=15.0)
    total = sum(s["end_seconds"] - s["start_seconds"] for s in segments)
    assert total == pytest.approx(3.0)


def test_invalid_target_raises():
    with pytest.raises(ValueError):
        select_segments([], target_duration_seconds=0)


def test_plan_status_declares_executor_implemented():
    status = plan_status()
    assert status["segment_planner"] == "implemented"
    assert status["segment_executor"] == "implemented"
