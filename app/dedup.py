"""降重（去重）分析：本地确定性检测，对齐各平台多级判重路径。

平台判重普遍为"文件指纹 → 抽帧指纹 → AI 同质化"多级漏斗
（见 configs/platform_profiles.yaml dedup_rules）。本模块覆盖前两级的
本地等价能力：
- 文件 MD5：最廉价的第一级查重（所有平台都会做）；
- 帧感知指纹（aHash）：对应关键帧/九宫格抽帧比对，用于批量内互相查重；
- 静态覆盖物检测：角落长期静态区域 → 疑似残留平台水印/角标（搬运痕迹）；
- 黑边检测：letterbox 黑边占比 → 疑似直接搬运未重制。
判重阈值为 provisional（行业资料归纳），待本地样本标定。
"""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np

from .platform_profiles import PlatformProfiles

HASH_GRID = 8  # aHash 8x8 = 64bit
CORNERS = ("top_left", "top_right", "bottom_left", "bottom_right")


def file_md5(path: str) -> str:
    """流式计算文件 MD5（平台第一级查重的本地等价物）。"""
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def frame_aphash(frame: np.ndarray) -> int:
    """灰度帧 → 64bit 感知指纹（aHash：8x8 块均值 + 全局均值阈值）。"""
    height, width = frame.shape[:2]
    row_edges = np.linspace(0, height, HASH_GRID + 1, dtype=int)
    col_edges = np.linspace(0, width, HASH_GRID + 1, dtype=int)
    cells: list[float] = []
    for r in range(HASH_GRID):
        for c in range(HASH_GRID):
            cell = frame[row_edges[r] : row_edges[r + 1], col_edges[c] : col_edges[c + 1]]
            cells.append(float(cell.mean()) if cell.size else 0.0)
    grid = np.asarray(cells)
    threshold = grid.mean()
    bits = 0
    for index, value in enumerate(grid):
        if value > threshold:
            bits |= 1 << index
    return bits


def fingerprint_similarity(hashes_a: list[int], hashes_b: list[int]) -> float:
    """两组帧指纹的对称相似度（0~1）。

    每个方向均计算「每帧在另一组里的最佳匹配」再取均值。单向匹配会让
    短片或重复帧只要被长片覆盖就虚高，因此最终取两个方向的较低值。
    顺序无关；任一组为空返回 0。
    """
    if not hashes_a or not hashes_b:
        return 0.0
    def best_match_mean(left: list[int], right: list[int]) -> float:
        return sum(
            max(
                1.0 - (hash_left ^ hash_right).bit_count() / (HASH_GRID * HASH_GRID)
                for hash_right in right
            )
            for hash_left in left
        ) / len(left)

    return round(min(best_match_mean(hashes_a, hashes_b), best_match_mean(hashes_b, hashes_a)), 4)


def analyze_static_overlay(
    frames: list[np.ndarray],
    *,
    variance_threshold: float = 0.002,
    corner_fraction: float = 0.25,
    dark_threshold: float = 0.08,
) -> dict[str, Any]:
    """时序静态区域分析：找跨帧几乎不变的像素（水印/黑边候选）。

    frames 为同一尺寸的灰度帧序列（0~1）。静态像素=时序方差低于阈值。
    角落静态占比高 → 疑似残留水印/角标；边缘整行暗且静态 → 黑边。
    """
    if not frames:
        raise ValueError("frames must not be empty")
    first_shape = frames[0].shape
    if len(first_shape) != 2 or any(frame.shape != first_shape for frame in frames):
        raise ValueError("frames must be same-sized grayscale arrays")
    if any(not np.isfinite(frame).all() for frame in frames):
        raise ValueError("frames must contain finite values")
    stack = np.stack(frames)
    variance = stack.var(axis=0)
    static_mask = variance < variance_threshold
    height, width = static_mask.shape

    corner_h = max(1, int(height * corner_fraction))
    corner_w = max(1, int(width * corner_fraction))
    corner_regions = {
        "top_left": static_mask[:corner_h, :corner_w],
        "top_right": static_mask[:corner_h, -corner_w:],
        "bottom_left": static_mask[-corner_h:, :corner_w],
        "bottom_right": static_mask[-corner_h:, -corner_w:],
    }
    corner_static_ratios = {
        name: round(float(region.mean()), 4) for name, region in corner_regions.items()
    }

    temporal_mean = stack.mean(axis=0)
    row_dark_static = (temporal_mean < dark_threshold) & static_mask
    row_ratios = row_dark_static.mean(axis=1)
    border_rows = int((row_ratios[:corner_h] > 0.9).sum()) + int(
        (row_ratios[-corner_h:] > 0.9).sum()
    )
    black_bar_ratio = round(border_rows / height, 4)

    return {
        "static_pixel_ratio": round(float(static_mask.mean()), 4),
        "corner_static_ratios": corner_static_ratios,
        "black_bar_ratio": black_bar_ratio,
    }


def evaluate_dedup(
    profiles: PlatformProfiles,
    *,
    similarity: float | None,
    overlay_metrics: dict[str, Any] | None,
) -> dict[str, Any]:
    """确定性判定：verdict(block>review>pass) + flags 清单。

    similarity：与参考片的帧指纹相似度（无参考片时传 None，跳过该判据）；
    overlay_metrics：analyze_static_overlay 输出（未检测时传 None）。
    """
    thresholds = profiles.thresholds
    flags: list[str] = []
    notes: list[str] = [f"dedup thresholds status: {profiles.status}"]
    verdict = "pass"

    if similarity is not None:
        if similarity >= thresholds["similarity_block"]:
            verdict = "block"
            flags.append("near_duplicate")
        elif similarity >= thresholds["similarity_review"]:
            verdict = "review"
            flags.append("high_similarity")
        notes.append(f"frame fingerprint similarity: {similarity}")

    if overlay_metrics is not None:
        corner_max = max(overlay_metrics["corner_static_ratios"].values())
        if corner_max >= thresholds["corner_static_watermark"]:
            flags.append("suspected_watermark_residual")
            if verdict == "pass":
                verdict = "review"
        if overlay_metrics["black_bar_ratio"] >= thresholds["black_bar_ratio_review"]:
            flags.append("suspected_black_bars")
            if verdict == "pass":
                verdict = "review"

    # 各平台差异化提示（命中 flags 时给出针对性降重建议）
    if flags:
        notes.append(
            "建议降重手段：重编码/改分辨率、裁切黑边与角标、变速、镜像、"
            "更换 BGM 与封面；小红书注意 3 个月时间窗（window_days）"
        )
    return {"verdict": verdict, "flags": flags, "notes": notes}
