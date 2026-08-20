"""文件持久化的本地批处理任务：L0 → L1/L2 → L3 成片。"""

from __future__ import annotations

import json
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .clip_executor import render_segments
from .clip_planner import ScoredWindow, select_segments
from .config import Settings
from .l2_referral import plan_referrals
from .l2_service import L2ServiceError, review_profiles
from .media import extract_jpeg_data_url
from .models import CandidateWindow, ClipSegment, CreateJobRequest, JobDetail, JobSummary, SignalsRequest
from .signals import compute_signals
from .style_profiles import RuleBook, match_profiles
from .titler import TitleContext, generate_titles, product_filename


TERMINAL = {"completed", "failed", "cancelled"}


class JobStore:
    def __init__(self, settings: Settings, rule_book: RuleBook | None):
        self.settings = settings
        self.rule_book = rule_book
        self.root = Path(settings.products_dir).resolve()
        self.jobs_root = self.root / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max(1, settings.job_workers))

    def create(self, request: CreateJobRequest) -> JobDetail:
        job_id = uuid.uuid4().hex
        now = _now()
        record: dict[str, Any] = {
            "job_id": job_id,
            "status": "queued",
            "video_path": request.video_path,
            "created_at": now,
            "updated_at": now,
            "request": request.model_dump(),
            "events": [{"at": now, "stage": "queued", "message": "job created"}],
            "result": {},
            "output_path": None,
            "error": None,
            "cancel_requested": False,
        }
        self._write(record)
        self._executor.submit(self._run, job_id)
        return _detail(record)

    def get(self, job_id: str) -> JobDetail | None:
        source = self._path(job_id)
        if not source.is_file():
            return None
        return _detail(json.loads(source.read_text(encoding="utf-8")))

    def list(self) -> list[JobSummary]:
        records = [json.loads(path.read_text(encoding="utf-8")) for path in self.jobs_root.glob("*.json")]
        return [_summary(record) for record in sorted(records, key=lambda item: item["created_at"], reverse=True)]

    def cancel(self, job_id: str) -> JobDetail | None:
        record = self._read(job_id)
        if record is None:
            return None
        if record["status"] not in TERMINAL:
            record["cancel_requested"] = True
            self._event(record, record["status"], "cancellation requested")
            self._write(record)
        return _detail(record)

    def retry(self, job_id: str) -> JobDetail | None:
        record = self._read(job_id)
        if record is None:
            return None
        if record["status"] not in {"failed", "cancelled"}:
            raise ValueError("only failed or cancelled jobs can be retried")
        record.update(status="queued", error=None, output_path=None, result={}, cancel_requested=False)
        self._event(record, "queued", "job retry queued")
        self._write(record)
        self._executor.submit(self._run, job_id)
        return _detail(record)

    def _run(self, job_id: str) -> None:
        record = self._read(job_id)
        if record is None:
            return
        try:
            request = CreateJobRequest(**record["request"])
            self._check_cancelled(record)
            self._set_status(record, "analyzing", "computing L0 signals and window candidates")
            signals_response = compute_signals(
                self.settings,
                SignalsRequest(video_path=request.video_path, motion_window_count=request.motion_window_count),
            )
            record["result"]["signals"] = signals_response.model_dump(mode="json")
            self._write(record)

            self._check_cancelled(record)
            self._set_status(record, "reviewing", "evaluating L1 profiles and L4 gatekeeper")
            review: dict[str, Any] = {"profiles": [], "referrals": [], "l2": {"status": "skipped"}}
            if self.rule_book is not None:
                matches, notes = match_profiles(self.rule_book, signals_response.signals)
                review["profiles"] = matches
                review["profile_notes"] = notes
                referrals = plan_referrals(self.rule_book, matches)
                review["referrals"] = referrals
                # 任务输入尚未包含 tagging 的片段级标签；不能将空标签的 pass
                # 误记为已完成合规审核。
                review["gatekeeper"] = {
                    "status": "not_evaluated",
                    "reason": "segment-level compliance labels are not available in this job",
                }
                selected_ids = request.profile_ids or [item["profile_id"] for item in referrals]
                if not request.request_l2:
                    review["l2"] = {"status": "skipped", "reason": "disabled by job request"}
                elif not selected_ids:
                    review["l2"] = {"status": "skipped", "reason": "no profile requested L2 review"}
                elif not self.settings.l2_enabled:
                    review["l2"] = {"status": "skipped", "reason": "L2 provider is not configured"}
                else:
                    try:
                        image_urls = [
                            extract_jpeg_data_url(
                                self.settings,
                                request.video_path,
                                timestamp_seconds=candidate.start_seconds,
                                width=self.settings.motion_frame_width,
                            )
                            for candidate in signals_response.candidates[:3]
                        ]
                        items = review_profiles(
                            self.settings,
                            self.rule_book,
                            signals=signals_response.signals,
                            candidates=signals_response.candidates,
                            profile_ids=selected_ids,
                            image_urls=image_urls,
                        )
                        review["l2"] = {"status": "completed", "items": [item.model_dump() for item in items]}
                    except L2ServiceError as exc:
                        review["l2"] = {"status": "skipped", "reason": str(exc)}
            else:
                review["reason"] = "rule book unavailable"
            record["result"]["review"] = review
            self._write(record)

            self._check_cancelled(record)
            self._set_status(record, "rendering", "planning and rendering MP4")
            segments = select_segments(
                [ScoredWindow(item.start_seconds, item.duration_seconds, item.score) for item in signals_response.candidates],
                target_duration_seconds=request.target_duration_seconds,
            )
            if not segments:
                raise RuntimeError("L0 produced no renderable candidate windows")
            clip_segments = [ClipSegment(**segment) for segment in segments]
            if request.output_name:
                output_name = _safe_name(request.output_name)
                record["result"]["title"] = Path(output_name).stem
            else:
                # 成品自动命名：风格线索 + L0 信号 → 可直接发布的剪辑标题
                style_hint = None
                if request.profile_ids:
                    style_hint = request.profile_ids[0]
                elif review.get("profiles"):
                    style_hint = review["profiles"][0]["profile_id"]
                context = TitleContext(
                    style_hint=style_hint,
                    duration_seconds=request.target_duration_seconds,
                    peak_motion=signals_response.signals.peak_motion_intensity,
                    audio_energy=float(signals_response.signals.audio_mean_volume_db or 0.0),
                )
                titles = generate_titles(context)
                record["result"]["title"] = titles[0]
                record["result"]["title_candidates"] = titles
                existing = {path.name for path in self.root.glob("*.mp4")}
                output_name = product_filename(titles[0], existing)
            record["title"] = record["result"].get("title")
            output = self.root / output_name
            render_segments(
                self.settings,
                video_path=request.video_path,
                segments=clip_segments,
                output_path=str(output),
                overwrite=True,
            )
            record["result"]["clip_plan"] = {"segments": [segment.model_dump() for segment in clip_segments]}
            record["output_path"] = str(output)
            self._set_status(record, "completed", "render completed")
        except _Cancelled:
            record["status"] = "cancelled"
            self._event(record, "cancelled", "job cancelled before next stage")
            self._write(record)
        except Exception as exc:  # Persist diagnostics for a later retry/hand-off.
            record["status"] = "failed"
            record["error"] = str(exc)
            record["result"].setdefault("traceback", traceback.format_exc())
            self._event(record, "failed", str(exc))
            self._write(record)

    def _check_cancelled(self, record: dict[str, Any]) -> None:
        latest = self._read(record["job_id"])
        if latest and latest.get("cancel_requested"):
            raise _Cancelled()

    def _set_status(self, record: dict[str, Any], status: str, message: str) -> None:
        record["status"] = status
        self._event(record, status, message)
        self._write(record)

    def _event(self, record: dict[str, Any], stage: str, message: str) -> None:
        record["updated_at"] = _now()
        record["events"].append({"at": record["updated_at"], "stage": stage, "message": message})

    def _path(self, job_id: str) -> Path:
        return self.jobs_root / f"{job_id}.json"

    def _read(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            source = self._path(job_id)
            return json.loads(source.read_text(encoding="utf-8")) if source.is_file() else None

    def _write(self, record: dict[str, Any]) -> None:
        with self._lock:
            target = self._path(record["job_id"])
            temp = target.with_suffix(".tmp")
            temp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(target)


class _Cancelled(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_name(name: str) -> str:
    base = Path(name).name
    return base if base.endswith(".mp4") else f"{base}.mp4"


def _summary(record: dict[str, Any]) -> JobSummary:
    return JobSummary(**{key: record.get(key) for key in JobSummary.model_fields})


def _detail(record: dict[str, Any]) -> JobDetail:
    values = {key: record.get(key) for key in JobDetail.model_fields}
    return JobDetail(**values)
