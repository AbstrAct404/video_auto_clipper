"""运动分析纯逻辑测试（不依赖 ffmpeg）。"""

from __future__ import annotations

import numpy as np
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models import MotionWindowRequest, NormalizedROI
from app.motion_service import (
    build_window_timestamps,
    classify_temporal,
    compare_pair,
)

SETTINGS = Settings()


def test_build_window_timestamps_3s_1fps():
    window = MotionWindowRequest(start_seconds=10.0, profile="window_3s_1fps")
    assert build_window_timestamps(window, duration_seconds=60.0) == [10.0, 11.0, 12.0]


def test_build_window_timestamps_burst_1s_3fps():
    window = MotionWindowRequest(start_seconds=0.0, profile="burst_1s_3fps")
    timestamps = build_window_timestamps(window, duration_seconds=60.0)
    assert len(timestamps) == 3
    assert timestamps[0] == 0.0


def test_build_window_timestamps_clips_to_duration():
    window = MotionWindowRequest(start_seconds=9.5, profile="window_3s_1fps")
    assert build_window_timestamps(window, duration_seconds=10.0) == [9.5]


def test_compare_pair_identical_frames_zero_diff():
    frame = np.full((8, 8), 0.5, dtype="float32")
    mean, ratio = compare_pair(
        frame, frame.copy(), roi=None, changed_pixel_threshold=0.1
    )
    assert mean == 0.0
    assert ratio == 0.0


def test_compare_pair_high_change():
    first = np.zeros((8, 8), dtype="float32")
    second = np.ones((8, 8), dtype="float32")
    mean, ratio = compare_pair(
        first, second, roi=None, changed_pixel_threshold=0.1
    )
    assert mean == pytest.approx(1.0)
    assert ratio == pytest.approx(1.0)


def test_compare_pair_shape_mismatch_raises():
    with pytest.raises(ValueError, match="dimensions must match"):
        compare_pair(
            np.zeros((8, 8), dtype="float32"),
            np.zeros((8, 9), dtype="float32"),
            roi=None,
            changed_pixel_threshold=0.1,
        )


def test_compare_pair_roi_applied():
    first = np.zeros((8, 8), dtype="float32")
    second = np.ones((8, 8), dtype="float32")
    roi = NormalizedROI(x_min=0.0, x_max=0.5, y_min=0.0, y_max=0.5)
    mean, ratio = compare_pair(
        first, second, roi=roi, changed_pixel_threshold=0.1
    )
    assert mean == pytest.approx(1.0)


def test_compare_pair_empty_roi_raises():
    frame = np.zeros((8, 8), dtype="float32")
    roi = NormalizedROI(x_min=0.0, x_max=0.01, y_min=0.0, y_max=0.01)
    with pytest.raises(ValueError, match="no pixels"):
        compare_pair(frame, frame.copy(), roi=roi, changed_pixel_threshold=0.1)


def test_roi_rejects_empty_region_at_contract_boundary():
    with pytest.raises(ValidationError, match="x_min < x_max"):
        NormalizedROI(x_min=0.5, x_max=0.5, y_min=0.0, y_max=1.0)


def test_classify_stable():
    summary = classify_temporal(
        [0.001, 0.002, 0.001], [0.01, 0.02, 0.01], settings=SETTINGS
    )
    assert summary.pattern == "stable"
    assert not summary.continuous_change


def test_classify_continuous():
    summary = classify_temporal(
        [0.1, 0.12, 0.09], [0.2, 0.25, 0.3], settings=SETTINGS
    )
    assert summary.pattern == "continuous"
    assert summary.continuous_change
    assert summary.active_pair_count == 3


def test_classify_intermittent():
    summary = classify_temporal(
        [0.1, 0.001, 0.1], [0.2, 0.01, 0.3], settings=SETTINGS
    )
    assert summary.pattern == "intermittent"


def test_classify_abrupt():
    summary = classify_temporal(
        [0.001, 0.5, 0.001], [0.01, 0.8, 0.01], settings=SETTINGS
    )
    assert summary.pattern == "abrupt"
