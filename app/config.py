"""运行配置：全部经环境变量注入，带确定性默认值。"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # 运动分析抽帧前统一降宽（与 Framework 场景检测 320px 同口径，控制 CPU 成本）
    motion_frame_width: int = 320
    # 运动判定阈值（对齐 Framework TemporalChangeAnalyzer 默认值）
    active_changed_pixel_ratio: float = 0.05
    abrupt_changed_pixel_ratio: float = 0.65
    abrupt_mean_absolute_difference: float = 0.35
    changed_pixel_threshold: float = 0.1
    # 视觉分析（SigLIP2 closed-set）：模型重、懒加载；默认关闭，dev 用 fake 模式
    visual_enabled: bool = False
    visual_fake_mode: bool = True
    # 信号层默认参数
    scene_cut_threshold: float = 0.3
    signal_motion_window_count: int = 4
    signal_luminance_fps: float = 1.0
    signal_luminance_max_frames: int = 120
    luminance_spike_threshold: float = 0.12
    # 规则书路径（L1 规则卡 / L4 门禁配置，相对路径以工作目录为基准）
    rule_book_path: str = "configs/style_profiles.yaml"
    # 平台画像路径（视频类型划分 + 各平台判重规则）
    platform_profiles_path: str = "configs/platform_profiles.yaml"
    # ffmpeg/ffprobe 可执行文件
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    ffmpeg_timeout_seconds: float = 300.0
    # L2 云端视觉复核（OpenAI-compatible Chat Completions；缺省关闭）
    l2_enabled: bool = False
    l2_api_key: str | None = None
    l2_base_url: str | None = None
    l2_model: str = "qwen-vl-plus"
    l2_timeout_seconds: float = 60.0
    # 本地任务与成片产物
    products_dir: str = "products"
    job_workers: int = 1

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            motion_frame_width=int(os.environ.get("SMARTCLIP_MOTION_WIDTH", "320")),
            visual_enabled=_env_flag("SMARTCLIP_VISUAL_ENABLED", False),
            visual_fake_mode=_env_flag("SMARTCLIP_VISUAL_FAKE", True),
            scene_cut_threshold=float(os.environ.get("SMARTCLIP_SCENE_THRESHOLD", "0.3")),
            signal_motion_window_count=int(
                os.environ.get("SMARTCLIP_MOTION_WINDOWS", "4")
            ),
            rule_book_path=os.environ.get(
                "SMARTCLIP_RULE_BOOK", "configs/style_profiles.yaml"
            ),
            platform_profiles_path=os.environ.get(
                "SMARTCLIP_PLATFORM_PROFILES", "configs/platform_profiles.yaml"
            ),
            l2_enabled=_env_flag("SMARTCLIP_L2_ENABLED", False),
            l2_api_key=os.environ.get("SMARTCLIP_L2_API_KEY"),
            l2_base_url=os.environ.get("SMARTCLIP_L2_BASE_URL"),
            l2_model=os.environ.get("SMARTCLIP_L2_MODEL", "qwen-vl-plus"),
            l2_timeout_seconds=float(os.environ.get("SMARTCLIP_L2_TIMEOUT_SECONDS", "60")),
            products_dir=os.environ.get("SMARTCLIP_PRODUCTS_DIR", "products"),
            job_workers=int(os.environ.get("SMARTCLIP_JOB_WORKERS", "1")),
        )
