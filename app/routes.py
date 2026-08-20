"""FastAPI 路由：运动分析 / 视觉分析 / L0 信号层 / L1 规则卡 / L2 移交 / L3 剪辑计划 / L4 门禁 / 平台画像与降重。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from .clip_executor import render_segments
from .clip_planner import ScoredWindow, plan_status, select_segments
from .dedup import (
    analyze_static_overlay,
    evaluate_dedup,
    file_md5,
    fingerprint_similarity,
    frame_aphash,
)
from .gatekeeper import evaluate_gatekeeper
from .l2_referral import plan_referrals
from .l2_service import L2ServiceError, review_profiles
from .job_service import JobStore
from .media import (
    MediaError,
    MediaInfo,
    extract_jpeg_data_url,
    probe_media,
    sample_gray_frames,
    sample_gray_frames_batch,
)
from .models import (
    NarrativeMatchItem,
    NarrativePlanRequest,
    NarrativePlanResponse,
    NarrativeSegment,
    ShotInfo,
    StoryboardRequest,
    StoryboardResponse,
    ClipPlanRequest,
    ClipPlanResponse,
    ClipRenderRequest,
    ClipRenderResponse,
    CreateJobRequest,
    ClipSegment,
    DedupAnalyzeRequest,
    DedupAnalyzeResponse,
    DedupCompareRequest,
    DedupCompareResponse,
    DedupSimilarityItem,
    GatekeeperCheckRequest,
    GatekeeperCheckResponse,
    JobDetail,
    JobListResponse,
    MotionAnalysisRequest,
    MotionAnalysisResponse,
    L2ReviewRequest,
    L2ReviewResponse,
    ProfileEvaluateRequest,
    ProfileEvaluateResponse,
    ProfileMatchItem,
    SignalsRequest,
    SignalsResponse,
    VisualAnalysisRequest,
    VisualAnalysisResponse,
    VisualEvidenceItem,
)
from .motion_service import analyze_motion
from .narrative import NarrativeTargetBook, match_targets, plan_narrative
from .storyboard import extract_storyboard
from .platform_profiles import PlatformProfiles
from .signals import compute_signals
from .style_profiles import RuleBook, RuleBookError, match_profiles
from .visual_service import answer_question, get_provider, resolve_catalog

router = APIRouter()


def _settings(request: Request):
    return request.app.state.settings


def _rule_book(request: Request) -> RuleBook:
    rule_book = getattr(request.app.state, "rule_book", None)
    if rule_book is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "rule book not loaded; check SMARTCLIP_RULE_BOOK and startup logs"
            ),
        )
    return rule_book


def _platform_profiles(request: Request) -> PlatformProfiles:
    profiles = getattr(request.app.state, "platform_profiles", None)
    if profiles is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "platform profiles not loaded; check SMARTCLIP_PLATFORM_PROFILES "
                "and startup logs"
            ),
        )
    return profiles


def _job_store(request: Request) -> JobStore:
    return request.app.state.job_store


def _narrative_book(request: Request) -> NarrativeTargetBook:
    book = getattr(request.app.state, "narrative_book", None)
    if book is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "narrative targets not loaded; check SMARTCLIP_NARRATIVE_TARGETS "
                "and startup logs"
            ),
        )
    return book


def _ensure_video(video_path: str) -> None:
    path = Path(video_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"video not found: {video_path}")


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
def ready(request: Request) -> dict[str, object]:
    settings = _settings(request)
    rule_book = getattr(request.app.state, "rule_book", None)
    return {
        "status": "ready",
        "features": {
            "motion_analysis": True,
            "visual_analysis": settings.visual_enabled,
            "visual_fake_mode": settings.visual_fake_mode,
            "signals": True,
            "profiles": rule_book is not None,
            "gatekeeper": rule_book is not None,
            "clip_planner": True,
            "clip_renderer": True,
            "l2_review": bool(settings.l2_enabled and settings.l2_api_key and settings.l2_base_url),
            "platforms": getattr(request.app.state, "platform_profiles", None) is not None,
            "dedup": getattr(request.app.state, "platform_profiles", None) is not None,
            "narrative": getattr(request.app.state, "narrative_book", None) is not None,
        },
    }


@router.post("/v1/jobs", response_model=JobDetail, status_code=202)
def create_job_route(request: Request, body: CreateJobRequest):
    """创建可持久化的 L0→L3 批处理任务；后台线程执行，不阻塞请求。"""
    _ensure_video(body.video_path)
    return _job_store(request).create(body)


@router.get("/v1/jobs", response_model=JobListResponse)
def list_jobs_route(request: Request):
    return JobListResponse(jobs=_job_store(request).list())


@router.get("/v1/jobs/{job_id}", response_model=JobDetail)
def get_job_route(request: Request, job_id: str):
    job = _job_store(request).get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    return job


@router.get("/v1/jobs/{job_id}/product", response_class=FileResponse)
def get_job_product_route(request: Request, job_id: str):
    """仅按任务记录返回其成片，供控制台直接预览。"""
    job = _job_store(request).get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    if not job.output_path:
        raise HTTPException(status_code=409, detail="job has no completed product")
    output = Path(job.output_path)
    if not output.is_file():
        raise HTTPException(status_code=404, detail="job product file not found")
    return FileResponse(output, media_type="video/mp4", filename=output.name)


@router.post("/v1/jobs/{job_id}/cancel", response_model=JobDetail)
def cancel_job_route(request: Request, job_id: str):
    job = _job_store(request).cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    return job


@router.post("/v1/jobs/{job_id}/retry", response_model=JobDetail, status_code=202)
def retry_job_route(request: Request, job_id: str):
    try:
        job = _job_store(request).retry(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if job is None:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    return job


@router.post("/v1/analyze/motion", response_model=MotionAnalysisResponse)
def analyze_motion_route(request: Request, body: MotionAnalysisRequest):
    """窗口级运动分析 HTTP 路由：采样 + absdiff + 时序变化分类。"""
    settings = _settings(request)
    _ensure_video(body.video_path)
    try:
        info = probe_media(settings, body.video_path)
        return analyze_motion(settings, body, info=info)
    except MediaError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/v1/analyze/visual", response_model=VisualAnalysisResponse)
def analyze_visual_route(request: Request, body: VisualAnalysisRequest):
    """closed-set 视觉分析 HTTP 路由：受限目录 + 余弦排序 + margin 拒答。"""
    settings = _settings(request)
    if not settings.visual_enabled:
        raise HTTPException(
            status_code=503,
            detail=(
                "visual analysis disabled; set SMARTCLIP_VISUAL_ENABLED=1 "
                "(and optionally SMARTCLIP_VISUAL_FAKE=0 for real SigLIP2)"
            ),
        )
    _ensure_video(body.video_path)
    try:
        info = probe_media(settings, body.video_path)
        if body.timestamps_seconds is None and body.window_start_seconds is None:
            raise HTTPException(
                status_code=400,
                detail="provide timestamps_seconds or window_start_seconds",
            )
        timestamps = body.timestamps_seconds or [
            round(body.window_start_seconds + offset, 3) for offset in (0.0, 1.0, 2.0)
        ]
        if any(ts >= info.duration_seconds for ts in timestamps):
            raise HTTPException(status_code=422, detail="timestamp outside duration")

        frames = sample_gray_frames(
            settings, body.video_path, list(timestamps), width=settings.motion_frame_width
        )
        catalog = resolve_catalog(body.catalog)
        provider = get_provider(settings)

        with tempfile.TemporaryDirectory(prefix="smartclip-visual-") as tmp:
            image_paths: list[Path] = []
            for ts in timestamps:
                frame = frames[ts]
                pgm = Path(tmp) / f"frame_{ts:.3f}.pgm"
                height, width = frame.shape
                payload = (frame * 255.0).clip(0, 255).astype("uint8").tobytes()
                pgm.write_bytes(
                    f"P5\n{width} {height}\n255\n".encode("ascii") + payload
                )
                image_paths.append(pgm)
            items: list[VisualEvidenceItem] = [
                answer_question(
                    provider,
                    question_type=question_type,
                    catalog=catalog,
                    image_paths=image_paths,
                    timestamps_seconds=list(timestamps),
                )
                for question_type in body.question_types
            ]
        return VisualAnalysisResponse(
            video_path=body.video_path, provider=provider.name, items=items
        )
    except HTTPException:
        raise
    except MediaError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/v1/signals/compute", response_model=SignalsResponse)
def compute_signals_route(request: Request, body: SignalsRequest):
    """L0 信号层：切镜率/运动强度/音频能量/亮度突变，本地零成本。"""
    settings = _settings(request)
    _ensure_video(body.video_path)
    try:
        return compute_signals(settings, body)
    except MediaError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/v1/profiles")
def list_profiles(request: Request) -> dict[str, object]:
    """规则书摘要：标定状态 + 规则卡清单 + 门禁维度（供前端/运维巡检）。"""
    rule_book = _rule_book(request)
    return {
        "schema_version": rule_book.schema_version,
        "source_path": rule_book.source_path,
        "calibration": {
            "status": rule_book.calibration.status,
            "sample_set": rule_book.calibration.sample_set,
            "sample_count": rule_book.calibration.sample_count,
            "next_review_at": rule_book.calibration.next_review_at,
        },
        "profiles": [
            {
                "profile_id": profile.profile_id,
                "display_name": profile.display_name,
                "enabled": profile.enabled,
                "hooks": list(profile.hooks),
                "l2_trigger": (
                    profile.l2_fallback.trigger if profile.l2_fallback else None
                ),
                # 剪辑模板倾向：供 chatbot/前端按风格选择剪辑策略
                "clip_strategy": dict(profile.clip_strategy),
            }
            for profile in rule_book.profiles
        ],
        "gatekeeper_dimensions": [
            dimension
            for dimension, config in rule_book.gatekeeper.items()
            if isinstance(config, dict)
        ],
    }


@router.post("/v1/profiles/evaluate", response_model=ProfileEvaluateResponse)
def evaluate_profiles_route(request: Request, body: ProfileEvaluateRequest):
    """L1 规则卡求值 + L2 移交计划：信号值 → 命中清单与云端复核清单。"""
    rule_book = _rule_book(request)
    try:
        matches, notes = match_profiles(
            rule_book, body.signals, production=body.production
        )
    except RuleBookError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    referrals = plan_referrals(rule_book, matches)
    return ProfileEvaluateResponse(
        rule_book_status=rule_book.calibration.status,
        matches=[ProfileMatchItem(**match) for match in matches],
        referrals=referrals,
        notes=notes,
    )


@router.post("/v1/gatekeeper/check", response_model=GatekeeperCheckResponse)
def gatekeeper_check_route(request: Request, body: GatekeeperCheckRequest):
    """L4 合规门禁：维度标签 → block/review/pass 确定性查表。"""
    rule_book = _rule_book(request)
    result = evaluate_gatekeeper(rule_book, body.labels)
    return GatekeeperCheckResponse(**result)


@router.post("/v1/clip/plan", response_model=ClipPlanResponse)
def clip_plan_route(body: ClipPlanRequest):
    """L3 剪辑计划：带分数窗口 → 15s 成片片段计划。"""
    windows = [
        ScoredWindow(
            start_seconds=window.start_seconds,
            duration_seconds=window.duration_seconds,
            score=window.score,
        )
        for window in body.windows
    ]
    segments = select_segments(
        windows,
        target_duration_seconds=body.target_duration_seconds,
        min_segment_seconds=body.min_segment_seconds,
    )
    return ClipPlanResponse(
        segments=[ClipSegment(**segment) for segment in segments],
        total_duration_seconds=round(
            sum(segment["end_seconds"] - segment["start_seconds"] for segment in segments),
            3,
        ),
        executor_status=plan_status(),
    )


@router.post("/v1/clip/render", response_model=ClipRenderResponse)
def clip_render_route(request: Request, body: ClipRenderRequest):
    """执行已确认的剪辑时间轴，输出可播放 MP4 成片。"""
    settings = _settings(request)
    _ensure_video(body.video_path)
    try:
        render_segments(
            settings,
            video_path=body.video_path,
            segments=body.segments,
            output_path=body.output_path,
            overwrite=body.overwrite,
        )
    except MediaError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ClipRenderResponse(
        video_path=body.video_path,
        output_path=body.output_path,
        segment_count=len(body.segments),
        total_duration_seconds=round(
            sum(segment.end_seconds - segment.start_seconds for segment in body.segments), 3
        ),
        executor_status={
            "segment_planner": "implemented",
            "segment_executor": "implemented",
            "transition_audio_blend": "not_implemented",
        },
    )


@router.post("/v1/l2/review", response_model=L2ReviewResponse)
def l2_review_route(request: Request, body: L2ReviewRequest):
    """将窗口级候选和受控提示词交给已配置的真实多模态 L2 服务。"""
    rule_book = _rule_book(request)
    settings = _settings(request)
    profile_ids = body.profile_ids
    if profile_ids is None:
        matches, _ = match_profiles(rule_book, body.signals)
        profile_ids = [item["profile_id"] for item in plan_referrals(rule_book, matches)]
    if not profile_ids:
        raise HTTPException(
            status_code=400,
            detail="no L2-referred profile matched; provide profile_ids explicitly",
        )
    image_urls = body.image_urls
    if not image_urls and body.video_path:
        _ensure_video(body.video_path)
        try:
            image_urls = [
                extract_jpeg_data_url(
                    settings,
                    body.video_path,
                    timestamp_seconds=candidate.start_seconds,
                    width=settings.motion_frame_width,
                )
                for candidate in sorted(
                    body.candidates, key=lambda candidate: (-candidate.score, candidate.start_seconds)
                )[:3]
            ]
        except MediaError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        items = review_profiles(
            settings,
            rule_book,
            signals=body.signals,
            candidates=body.candidates,
            profile_ids=profile_ids,
            image_urls=image_urls,
        )
    except L2ServiceError as exc:
        status = 503 if not settings.l2_enabled else 502
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return L2ReviewResponse(items=items)


# ---------- 平台画像 / 降重 ----------

def _uniform_frames(settings, video_path: str, info: MediaInfo, max_frames: int):
    """全片均匀抽灰度帧（降重指纹/静态区域分析共用）。"""
    count = max(4, min(max_frames, max(4, int(info.duration_seconds))))
    timestamps = [
        round(info.duration_seconds * (i + 0.5) / count, 3) for i in range(count)
    ]
    source_size = (info.width, info.height) if info.width and info.height else None
    frames = sample_gray_frames_batch(
        settings,
        video_path,
        timestamps,
        width=settings.motion_frame_width,
        source_size=source_size,
        max_frames=max_frames,
    )
    return [frames[ts] for ts in timestamps if ts in frames]


@router.get("/v1/platforms")
def list_platforms(request: Request) -> dict[str, object]:
    """平台画像摘要：统一类目→各平台映射 + 发布规格 + 判重规则（调研所得）。"""
    profiles = _platform_profiles(request)
    return {
        "schema_version": profiles.schema_version,
        "status": profiles.status,
        "updated_at": profiles.updated_at,
        "source_path": profiles.source_path,
        "unified_categories": [
            {
                "id": category["id"],
                "display_name": category.get("display_name", category["id"]),
                "platforms": category["platforms"],
            }
            for category in profiles.unified_categories
        ],
        "platform_specs": profiles.platform_specs,
        "bilibili_tid_reference": {
            str(tid): name for tid, name in profiles.bilibili_tid_reference.items()
        },
        "dedup_common_stages": list(profiles.dedup_common_stages),
        "dedup_platforms": profiles.dedup_platforms,
        "thresholds": profiles.thresholds,
        "sources": list(profiles.sources),
    }


@router.post("/v1/dedup/analyze", response_model=DedupAnalyzeResponse)
def dedup_analyze_route(request: Request, body: DedupAnalyzeRequest):
    """降重体检：MD5 + 帧指纹 + 残留水印/黑边检测（对齐平台前两级判重）。"""
    settings = _settings(request)
    profiles = _platform_profiles(request)
    _ensure_video(body.video_path)
    try:
        info = probe_media(settings, body.video_path)
        frames = _uniform_frames(settings, body.video_path, info, body.max_frames)
        overlay = analyze_static_overlay(frames)
        verdict = evaluate_dedup(
            profiles, similarity=None, overlay_metrics=overlay
        )
        return DedupAnalyzeResponse(
            video_path=body.video_path,
            md5=file_md5(body.video_path),
            frame_count=len(frames),
            fingerprints=[format(frame_aphash(frame), "016x") for frame in frames],
            static_pixel_ratio=overlay["static_pixel_ratio"],
            corner_static_ratios=overlay["corner_static_ratios"],
            black_bar_ratio=overlay["black_bar_ratio"],
            verdict=verdict["verdict"],
            flags=verdict["flags"],
            notes=verdict["notes"],
        )
    except MediaError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/v1/dedup/compare", response_model=DedupCompareResponse)
def dedup_compare_route(request: Request, body: DedupCompareRequest):
    """重复率比对：与参考片（如已发布成片/原片）帧指纹相似度 → 判定。"""
    settings = _settings(request)
    profiles = _platform_profiles(request)
    _ensure_video(body.video_path)
    for reference in body.reference_paths:
        _ensure_video(reference)
    try:
        info = probe_media(settings, body.video_path)
        main_hashes = [
            frame_aphash(frame)
            for frame in _uniform_frames(settings, body.video_path, info, body.max_frames)
        ]
        similarities: list[DedupSimilarityItem] = []
        for reference in body.reference_paths:
            ref_info = probe_media(settings, reference)
            ref_hashes = [
                frame_aphash(frame)
                for frame in _uniform_frames(settings, reference, ref_info, body.max_frames)
            ]
            similarities.append(
                DedupSimilarityItem(
                    reference_path=reference,
                    similarity=fingerprint_similarity(main_hashes, ref_hashes),
                )
            )
        max_similarity = max(item.similarity for item in similarities)
        verdict = evaluate_dedup(
            profiles, similarity=max_similarity, overlay_metrics=None
        )
        return DedupCompareResponse(
            video_path=body.video_path,
            similarities=similarities,
            max_similarity=max_similarity,
            verdict=verdict["verdict"],
            flags=verdict["flags"],
            notes=verdict["notes"],
        )
    except MediaError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------- 分镜捕捉 / 叙事剪辑 ----------


@router.get("/v1/narrative")
def narrative_book_summary(request: Request) -> dict[str, object]:
    """叙事目标与模板摘要：风格→画面目标 + 混剪模板 + 标定状态。"""
    book = _narrative_book(request)
    return {
        "schema_version": book.schema_version,
        "calibration_status": book.calibration_status,
        "source_path": book.source_path,
        "targets": [
            {
                "target_id": target.target_id,
                "display_name": target.display_name,
                "styles": list(target.styles),
                "description": target.description,
                "match": target.match,
                "l2_hint": target.l2_hint,
            }
            for target in book.targets
        ],
        "templates": [
            {
                "template_id": template.template_id,
                "display_name": template.display_name,
                "description": template.description,
                "strategy": template.strategy,
            }
            for template in book.templates
        ],
        "interleave": {
            "enabled": book.interleave.enabled,
            "max_pairs": book.interleave.max_pairs,
            "min_pair_seconds": book.interleave.min_pair_seconds,
        },
    }


@router.post("/v1/storyboard/extract", response_model=StoryboardResponse)
def storyboard_extract_route(request: Request, body: StoryboardRequest):
    """剪辑前分镜捕捉：切点分镜 + 逐镜多帧采样 + 镜头级运动/亮度信号。"""
    settings = _settings(request)
    _ensure_video(body.video_path)
    try:
        info, shots = extract_storyboard(
            settings,
            body.video_path,
            frames_per_shot=body.frames_per_shot,
            max_frames=body.max_frames,
            scene_threshold=body.scene_threshold,
        )
    except MediaError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return StoryboardResponse(
        video_path=body.video_path,
        duration_seconds=info.duration_seconds,
        shot_count=len(shots),
        shots=[
            ShotInfo(
                index=shot.index,
                start_seconds=shot.start_seconds,
                end_seconds=shot.end_seconds,
                duration_seconds=shot.duration_seconds,
                sampled_frames=shot.sampled_frames,
                mean_motion_intensity=shot.mean_motion_intensity,
                peak_motion_intensity=shot.peak_motion_intensity,
                luminance_spike_ratio=shot.luminance_spike_ratio,
                luminance_delta_max=shot.luminance_delta_max,
            )
            for shot in shots
        ],
    )


@router.post("/v1/narrative/plan", response_model=NarrativePlanResponse)
def narrative_plan_route(request: Request, body: NarrativePlanRequest):
    """叙事剪辑计划：分镜 → 风格目标匹配 → 模板重排（可混剪、可人物-行动交错）。

    style_id 可选：给定后只评估归属该风格的目标；建议先跑
    /v1/signals/compute + /v1/profiles/evaluate 判断整体风格再传入。
    """
    settings = _settings(request)
    book = _narrative_book(request)
    _ensure_video(body.video_path)
    try:
        info, shots = extract_storyboard(
            settings,
            body.video_path,
            frames_per_shot=body.frames_per_shot,
            max_frames=body.max_frames,
        )
        matches = match_targets(book, shots, style_id=body.style_id)
        segments, notes = plan_narrative(
            book,
            matches,
            body.template_id,
            target_duration_seconds=body.target_duration_seconds,
            interleave=body.interleave,
        )
    except MediaError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return NarrativePlanResponse(
        video_path=body.video_path,
        style_id=body.style_id,
        template_id=body.template_id,
        calibration_status=book.calibration_status,
        shot_count=len(shots),
        matches=[
            NarrativeMatchItem(
                target_id=match.target_id,
                kind=match.kind,
                shot_indexes=list(match.shot_indexes),
                start_seconds=match.start_seconds,
                end_seconds=match.end_seconds,
                score=match.score,
            )
            for match in matches[:64]
        ],
        segments=[NarrativeSegment(**segment) for segment in segments],
        total_duration_seconds=round(
            sum(item["end_seconds"] - item["start_seconds"] for item in segments), 3
        ),
        notes=notes,
    )
