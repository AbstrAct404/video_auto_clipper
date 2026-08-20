"""平台画像加载器 + 降重纯逻辑测试（不依赖 ffmpeg）。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.dedup import (
    analyze_static_overlay,
    evaluate_dedup,
    fingerprint_similarity,
    frame_aphash,
)
from app.platform_profiles import (
    PlatformProfilesError,
    load_platform_profiles,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROFILES_PATH = PROJECT_ROOT / "configs" / "platform_profiles.yaml"


def _profiles():
    return load_platform_profiles(PROFILES_PATH)


# ---------- 加载器 ----------

def test_load_shipped_platform_profiles():
    profiles = _profiles()
    assert profiles.schema_version == "platform-profiles-1.0"
    assert profiles.status == "provisional"
    ids = {category["id"] for category in profiles.unified_categories}
    assert {"drama_clip", "hype_mashup", "fast_dialogue", "emotional", "comedy"} <= ids
    # 用户要求覆盖的平台都应在判重规则里
    assert {"douyin", "kuaishou", "bilibili", "xiaohongshu", "instagram", "x", "youtube"} <= set(
        profiles.dedup_platforms
    )
    # B 站官方分区 tid 参考存在
    assert profiles.bilibili_tid_reference[181] == "影视"
    # 小红书 3 个月时间窗
    assert profiles.dedup_platforms["xiaohongshu"]["window_days"] == 90


def test_missing_file_raises():
    with pytest.raises(PlatformProfilesError, match="not found"):
        load_platform_profiles("/nonexistent/platform_profiles.yaml")


def test_reject_review_threshold_not_lower(tmp_path):
    text = PROFILES_PATH.read_text(encoding="utf-8").replace(
        "similarity_review: 0.75", "similarity_review: 0.95"
    )
    bad = tmp_path / "platform_profiles.yaml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(PlatformProfilesError, match="similarity_review"):
        load_platform_profiles(bad)


def test_reject_missing_threshold_key(tmp_path):
    text = PROFILES_PATH.read_text(encoding="utf-8").replace(
        "black_bar_ratio_review: 0.12", "black_bar_ratio_typo: 0.12"
    )
    bad = tmp_path / "platform_profiles.yaml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(PlatformProfilesError, match="missing keys"):
        load_platform_profiles(bad)


# ---------- 帧指纹 ----------

def test_aphash_identical_frames_match():
    rng = np.random.default_rng(7)
    frame = rng.random((64, 64)).astype("float32")
    assert frame_aphash(frame) == frame_aphash(frame.copy())


def test_aphash_robust_to_small_brightness_shift():
    rng = np.random.default_rng(7)
    frame = rng.random((64, 64)).astype("float32") * 0.6 + 0.2
    shifted = np.clip(frame + 0.05, 0, 1)
    similarity = fingerprint_similarity([frame_aphash(frame)], [frame_aphash(shifted)])
    assert similarity > 0.8  # 感知指纹对整体亮度漂移鲁棒


def test_fingerprint_similarity_disjoint():
    rng = np.random.default_rng(11)
    frame_a = rng.random((32, 32)).astype("float32")
    frame_b = 1.0 - frame_a  # 反相 → 指纹近似互补
    similarity = fingerprint_similarity([frame_aphash(frame_a)], [frame_aphash(frame_b)])
    assert similarity < 0.5
    assert fingerprint_similarity([], [1]) == 0.0


# ---------- 静态覆盖物 / 黑边 ----------

def _moving_frames_with_corner_patch(patch: bool, black_bars: bool):
    rng = np.random.default_rng(3)
    frames = []
    for _ in range(8):
        frame = rng.random((40, 40)).astype("float32") * 0.8 + 0.1
        if patch:
            frame[:12, :12] = 0.95  # 左上角恒定亮块（模拟残留水印）
        if black_bars:
            frame[:6, :] = 0.0  # 顶部恒定黑边
        frames.append(frame)
    return frames


def test_overlay_detects_corner_watermark():
    metrics = analyze_static_overlay(_moving_frames_with_corner_patch(True, False))
    assert metrics["corner_static_ratios"]["top_left"] > 0.9
    assert metrics["corner_static_ratios"]["bottom_right"] < 0.1
    assert metrics["black_bar_ratio"] == 0.0


def test_overlay_detects_black_bars():
    metrics = analyze_static_overlay(_moving_frames_with_corner_patch(False, True))
    assert metrics["black_bar_ratio"] == pytest.approx(6 / 40, abs=1e-3)


def test_overlay_empty_frames_raises():
    with pytest.raises(ValueError):
        analyze_static_overlay([])


# ---------- 判定 ----------

def test_evaluate_dedup_block_on_high_similarity():
    result = evaluate_dedup(_profiles(), similarity=0.95, overlay_metrics=None)
    assert result["verdict"] == "block"
    assert "near_duplicate" in result["flags"]


def test_evaluate_dedup_review_on_medium_similarity():
    result = evaluate_dedup(_profiles(), similarity=0.8, overlay_metrics=None)
    assert result["verdict"] == "review"
    assert "high_similarity" in result["flags"]


def test_evaluate_dedup_pass_when_clean():
    metrics = analyze_static_overlay(_moving_frames_with_corner_patch(False, False))
    result = evaluate_dedup(_profiles(), similarity=0.2, overlay_metrics=metrics)
    assert result["verdict"] == "pass"
    assert result["flags"] == []


def test_evaluate_dedup_flags_watermark_residual():
    metrics = analyze_static_overlay(_moving_frames_with_corner_patch(True, False))
    result = evaluate_dedup(_profiles(), similarity=None, overlay_metrics=metrics)
    assert result["verdict"] == "review"
    assert "suspected_watermark_residual" in result["flags"]
