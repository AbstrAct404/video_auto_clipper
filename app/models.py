"""HTTP 数据契约（Pydantic v2，extra="forbid" 严格校验）。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SamplingProfile = Literal["burst_1s_3fps", "window_3s_1fps"]
TemporalPattern = Literal["stable", "intermittent", "continuous", "abrupt"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------- 运动分析 ----------

class NormalizedROI(StrictModel):
    """归一化感兴趣区域（0~1 坐标）。"""

    x_min: float = Field(ge=0, le=1)
    x_max: float = Field(ge=0, le=1)
    y_min: float = Field(ge=0, le=1)
    y_max: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_non_empty(self) -> "NormalizedROI":
        if self.x_min >= self.x_max or self.y_min >= self.y_max:
            raise ValueError("ROI must satisfy x_min < x_max and y_min < y_max")
        return self


class MotionWindowRequest(StrictModel):
    start_seconds: float = Field(ge=0)
    profile: SamplingProfile = "window_3s_1fps"
    roi: NormalizedROI | None = None


class PixelDifference(StrictModel):
    first_timestamp_seconds: float
    second_timestamp_seconds: float
    mean_absolute_difference: float
    changed_pixel_ratio: float


class TemporalSummary(StrictModel):
    pattern: TemporalPattern
    continuous_change: bool
    active_pair_count: int
    pair_count: int
    mean_absolute_difference: float
    max_absolute_difference: float
    mean_changed_pixel_ratio: float
    peak_changed_pixel_ratio: float


class MotionWindowResult(StrictModel):
    start_seconds: float
    duration_seconds: float
    profile: SamplingProfile
    status: Literal["completed", "insufficient_data", "outside_duration"]
    temporal_summary: TemporalSummary | None = None
    pixel_differences: list[PixelDifference] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class MotionAnalysisRequest(StrictModel):
    video_path: str = Field(min_length=1)
    windows: list[MotionWindowRequest] = Field(min_length=1, max_length=32)
    frame_width: int | None = Field(default=None, ge=64, le=1280)


class MotionAnalysisResponse(StrictModel):
    video_path: str
    duration_seconds: float
    frame_width: int
    windows: list[MotionWindowResult]


# ---------- 视觉分析（closed-set） ----------

class VisualAnalysisRequest(StrictModel):
    video_path: str = Field(min_length=1)
    # 二选一：显式帧时间戳（2~3 个）或窗口起点（自动取窗内 2~3 帧）
    timestamps_seconds: list[Annotated[float, Field(ge=0)]] | None = Field(
        default=None, min_length=2, max_length=3
    )
    window_start_seconds: float | None = Field(default=None, ge=0)
    question_types: list[str] = Field(min_length=1, max_length=16)
    # 允许按请求扩展目录（受限规模），缺省用内置目录
    catalog: dict[str, dict[str, str]] | None = None

    @model_validator(mode="after")
    def validate_time_selector(self) -> "VisualAnalysisRequest":
        if (self.timestamps_seconds is None) == (self.window_start_seconds is None):
            raise ValueError(
                "provide exactly one of timestamps_seconds or window_start_seconds"
            )
        return self


class RankedVisualCandidate(StrictModel):
    value: str
    prompt: str
    score: float


class VisualEvidenceItem(StrictModel):
    question_type: str
    status: Literal["answered", "abstained"]
    answer: str | None = None
    confidence: float
    margin: float
    timestamps_seconds: list[float]
    ranked_candidates: list[RankedVisualCandidate]
    notes: list[str] = Field(default_factory=list)


class VisualAnalysisResponse(StrictModel):
    video_path: str
    provider: str
    items: list[VisualEvidenceItem]


# ---------- L0 信号层 ----------

class SignalsRequest(StrictModel):
    video_path: str = Field(min_length=1)
    scene_threshold: float | None = Field(default=None, ge=0.05, le=0.9)
    motion_window_count: int | None = Field(default=None, ge=1, le=16)


class SignalValues(StrictModel):
    shot_cut_rate_per_min: float
    scene_count: int
    mean_motion_intensity: float
    peak_motion_intensity: float
    continuous_motion_window_ratio: float
    audio_mean_volume_db: float | None = None
    luminance_spike_ratio: float
    duration_seconds: float
    width: int | None = None
    height: int | None = None
    fps: float | None = None


class SignalsResponse(StrictModel):
    video_path: str
    schema_version: str = "signals-1.0"
    signals: SignalValues
    candidates: list["CandidateWindow"] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class CandidateWindow(StrictModel):
    """可直接进入 L2 复核或 L3 剪辑计划的窗口级候选。"""

    start_seconds: float = Field(ge=0)
    duration_seconds: float = Field(gt=0)
    score: float = Field(ge=0, le=1)
    motion_intensity: float = Field(ge=0, le=1)
    peak_motion_intensity: float = Field(ge=0, le=1)
    temporal_pattern: TemporalPattern
    luminance_spike_ratio: float = Field(ge=0, le=1)
    score_components: dict[str, float] = Field(default_factory=dict)


# ---------- L1 规则卡求值 / L2 移交 / L4 门禁 / L3 剪辑计划 ----------

class ProfileEvaluateRequest(StrictModel):
    signals: SignalValues
    production: bool = False


class ProfileMatchItem(StrictModel):
    profile_id: str
    display_name: str
    style_score: float
    hooks: list[str] = Field(default_factory=list)
    clip_strategy: dict[str, object] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class L2ReferralItem(StrictModel):
    profile_id: str
    prompt_ref: str
    trigger: Literal["always", "ambiguous_multi_hit"]
    max_windows: int
    reason: Literal["multi_hit_ambiguous", "profile_requires_l2"]
    rank: int


class L2ReviewRequest(StrictModel):
    signals: SignalValues
    candidates: list[CandidateWindow] = Field(min_length=1, max_length=32)
    profile_ids: list[str] | None = Field(default=None, min_length=1, max_length=8)
    video_path: str | None = Field(default=None, min_length=1)
    image_urls: list[str] = Field(default_factory=list, max_length=8)


class L2ReviewItem(StrictModel):
    profile_id: str
    prompt_ref: str
    model: str
    result: dict[str, object]
    raw_content: str


class L2ReviewResponse(StrictModel):
    schema_version: str = "l2-review-1.0"
    items: list[L2ReviewItem]


class ProfileEvaluateResponse(StrictModel):
    schema_version: str = "rules-eval-1.0"
    rule_book_status: str
    matches: list[ProfileMatchItem]
    referrals: list[L2ReferralItem]
    notes: list[str] = Field(default_factory=list)


class GatekeeperCheckRequest(StrictModel):
    labels: dict[str, str | list[str] | None]


class GatekeeperCheckResponse(StrictModel):
    schema_version: str = "gatekeeper-1.0"
    verdict: Literal["block", "review", "pass"]
    block_hits: list[str] = Field(default_factory=list)
    review_hits: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ClipWindowScore(StrictModel):
    start_seconds: float = Field(ge=0)
    duration_seconds: float = Field(gt=0)
    score: float


class ClipPlanRequest(StrictModel):
    windows: list[ClipWindowScore] = Field(min_length=1, max_length=64)
    target_duration_seconds: float = Field(default=15.0, gt=0, le=60)
    min_segment_seconds: float = Field(default=1.0, ge=0)


class ClipSegment(StrictModel):
    start_seconds: float
    end_seconds: float
    score: float


class ClipPlanResponse(StrictModel):
    schema_version: str = "clip-plan-1.0"
    segments: list[ClipSegment]
    total_duration_seconds: float
    executor_status: dict[str, str]


class ClipRenderRequest(StrictModel):
    video_path: str = Field(min_length=1)
    segments: list[ClipSegment] = Field(min_length=1, max_length=32)
    output_path: str = Field(min_length=1)
    overwrite: bool = False


class ClipRenderResponse(StrictModel):
    schema_version: str = "clip-render-1.0"
    video_path: str
    output_path: str
    segment_count: int
    total_duration_seconds: float
    executor_status: dict[str, str]


# ---------- 异步批处理任务 ----------

JobStatus = Literal[
    "queued", "analyzing", "reviewing", "rendering", "completed", "failed", "cancelled"
]


class CreateJobRequest(StrictModel):
    video_path: str = Field(min_length=1)
    target_duration_seconds: float = Field(default=15.0, gt=0, le=60)
    motion_window_count: int = Field(default=8, ge=2, le=16)
    profile_ids: list[str] | None = Field(default=None, min_length=1, max_length=8)
    request_l2: bool = True
    output_name: str | None = Field(default=None, min_length=1, max_length=120)


class JobSummary(StrictModel):
    job_id: str
    status: JobStatus
    video_path: str
    created_at: str
    updated_at: str
    output_path: str | None = None
    error: str | None = None
    title: str | None = None


class JobDetail(JobSummary):
    events: list[dict[str, object]] = Field(default_factory=list)
    result: dict[str, object] = Field(default_factory=dict)


class JobListResponse(StrictModel):
    jobs: list[JobSummary]


# ---------- 平台画像 / 降重分析 ----------

class DedupAnalyzeRequest(StrictModel):
    video_path: str = Field(min_length=1)
    max_frames: int = Field(default=24, ge=4, le=64)


class DedupAnalyzeResponse(StrictModel):
    schema_version: str = "dedup-1.0"
    video_path: str
    md5: str
    frame_count: int
    fingerprints: list[str] = Field(default_factory=list)  # 帧 aHash 指纹（hex）
    static_pixel_ratio: float
    corner_static_ratios: dict[str, float]
    black_bar_ratio: float
    verdict: Literal["block", "review", "pass"]
    flags: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class DedupCompareRequest(StrictModel):
    video_path: str = Field(min_length=1)
    reference_paths: list[str] = Field(min_length=1, max_length=8)
    max_frames: int = Field(default=24, ge=4, le=64)


class DedupSimilarityItem(StrictModel):
    reference_path: str
    similarity: float


class DedupCompareResponse(StrictModel):
    schema_version: str = "dedup-compare-1.0"
    video_path: str
    similarities: list[DedupSimilarityItem]
    max_similarity: float
    verdict: Literal["block", "review", "pass"]
    flags: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ---------- 分镜捕捉 / 叙事剪辑 ----------


class StoryboardRequest(StrictModel):
    video_path: str = Field(min_length=1)
    frames_per_shot: int = Field(default=3, ge=1, le=8)
    max_frames: int = Field(default=240, ge=16, le=960)
    scene_threshold: float | None = Field(default=None, gt=0, lt=1)


class ShotInfo(StrictModel):
    index: int
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    duration_seconds: float = Field(gt=0)
    sampled_frames: int = Field(ge=0)
    mean_motion_intensity: float = Field(ge=0)
    peak_motion_intensity: float = Field(ge=0)
    luminance_spike_ratio: float = Field(ge=0, le=1)
    luminance_delta_max: float = Field(ge=0, le=1)


class StoryboardResponse(StrictModel):
    schema_version: str = "storyboard-1.0"
    video_path: str
    duration_seconds: float
    shot_count: int
    shots: list[ShotInfo]


class NarrativePlanRequest(StrictModel):
    video_path: str = Field(min_length=1)
    style_id: str | None = Field(default=None, min_length=1)
    template_id: str = Field(default="hook_first", min_length=1)
    target_duration_seconds: float = Field(default=15.0, gt=0, le=60)
    interleave: bool = False
    frames_per_shot: int = Field(default=3, ge=1, le=8)
    max_frames: int = Field(default=240, ge=16, le=960)


class NarrativeMatchItem(StrictModel):
    target_id: str
    kind: Literal["shot", "pair"]
    shot_indexes: list[int] = Field(min_length=1, max_length=2)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    score: float


class NarrativeSegment(StrictModel):
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    shot_indexes: list[int] = Field(min_length=1, max_length=2)
    target_ids: list[str] = Field(min_length=1)
    score: float
    role: str


class NarrativePlanResponse(StrictModel):
    schema_version: str = "narrative-plan-1.0"
    video_path: str
    style_id: str | None
    template_id: str
    calibration_status: str
    shot_count: int
    matches: list[NarrativeMatchItem] = Field(default_factory=list, max_length=64)
    segments: list[NarrativeSegment] = Field(default_factory=list)
    total_duration_seconds: float = 0.0
    notes: list[str] = Field(default_factory=list)


class TitlePreviewRequest(StrictModel):
    video_path: str = Field(min_length=1)
    style_id: str | None = Field(default=None, max_length=40)
    target_duration_seconds: float = Field(default=15.0, gt=0, le=60)
    # L2 narrative_board 标注的人物-行动线索（可选，注入个性化标题）
    character_actions: list[dict[str, str]] | None = Field(default=None, max_length=4)


class TitlePreviewResponse(StrictModel):
    schema_version: str = "title-preview-1.0"
    video_path: str
    style_id: str | None
    recommended: str
    candidates: list[str] = Field(default_factory=list)
    matched_target_ids: list[str] = Field(default_factory=list)
    filename: str
