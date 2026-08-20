"""API 集成测试（TestClient + ffmpeg 生成的 fixture 视频）。"""

from __future__ import annotations

import pytest
import time
from pathlib import Path
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture()
def client_visual_off() -> TestClient:
    return TestClient(create_app(Settings()))


@pytest.fixture()
def client_visual_fake() -> TestClient:
    return TestClient(
        create_app(Settings(visual_enabled=True, visual_fake_mode=True))
    )


def test_health(client_visual_off):
    response = client_visual_off.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_console_page_is_served(client_visual_off):
    response = client_visual_off.get("/")
    assert response.status_code == 200
    assert "智能剪辑控制台" in response.text


def test_ready_reports_features(client_visual_off):
    response = client_visual_off.get("/ready")
    assert response.status_code == 200
    features = response.json()["features"]
    assert features["motion_analysis"] is True
    assert features["visual_analysis"] is False


def test_motion_route_missing_file_404(client_visual_off):
    response = client_visual_off.post(
        "/v1/analyze/motion",
        json={"video_path": "/nonexistent.mp4", "windows": [{"start_seconds": 0}]},
    )
    assert response.status_code == 404


def test_motion_route_rejects_extra_fields(client_visual_off, small_video):
    response = client_visual_off.post(
        "/v1/analyze/motion",
        json={
            "video_path": str(small_video),
            "windows": [{"start_seconds": 0}],
            "unexpected": 1,
        },
    )
    assert response.status_code == 422  # extra="forbid"


def test_motion_route_completed_window(client_visual_off, small_video):
    response = client_visual_off.post(
        "/v1/analyze/motion",
        json={
            "video_path": str(small_video),
            "windows": [
                {"start_seconds": 0.2, "profile": "window_3s_1fps"},
                {"start_seconds": 99.0, "profile": "window_3s_1fps"},
            ],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["duration_seconds"] > 3.0
    first, second = payload["windows"]
    assert first["status"] == "completed"
    assert first["temporal_summary"]["pair_count"] == 2
    assert first["temporal_summary"]["pattern"] in {
        "stable", "intermittent", "continuous", "abrupt",
    }
    assert second["status"] == "outside_duration"


def test_visual_route_disabled_returns_503(client_visual_off, small_video):
    response = client_visual_off.post(
        "/v1/analyze/visual",
        json={
            "video_path": str(small_video),
            "window_start_seconds": 0.5,
            "question_types": ["shot_size"],
        },
    )
    assert response.status_code == 503


def test_visual_route_fake_mode_end_to_end(client_visual_fake, small_video):
    response = client_visual_fake.post(
        "/v1/analyze/visual",
        json={
            "video_path": str(small_video),
            "timestamps_seconds": [0.5, 1.0, 1.5],
            "question_types": ["shot_size", "mood"],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "fake"
    assert len(payload["items"]) == 2
    for item in payload["items"]:
        assert item["status"] in {"answered", "abstained"}
        assert item["ranked_candidates"]


def test_visual_route_unknown_question_type_400(client_visual_fake, small_video):
    response = client_visual_fake.post(
        "/v1/analyze/visual",
        json={
            "video_path": str(small_video),
            "window_start_seconds": 0.5,
            "question_types": ["no_such_group"],
        },
    )
    assert response.status_code == 400


def test_signals_route_end_to_end(client_visual_off, small_video):
    response = client_visual_off.post(
        "/v1/signals/compute", json={"video_path": str(small_video)}
    )
    assert response.status_code == 200
    payload = response.json()
    signals = payload["signals"]
    assert payload["schema_version"] == "signals-1.0"
    assert signals["duration_seconds"] > 3.0
    # fixture 在 2s 处有一次硬切
    assert signals["scene_count"] >= 1
    assert signals["shot_cut_rate_per_min"] > 0
    assert 0.0 <= signals["mean_motion_intensity"] <= 1.0
    assert signals["audio_mean_volume_db"] is not None
    assert signals["width"] == 128
    assert payload["candidates"]
    assert payload["candidates"] == sorted(
        payload["candidates"], key=lambda item: (-item["score"], item["start_seconds"])
    )


def test_clip_render_route(client_visual_off, small_video, tmp_path):
    output = tmp_path / "rendered.mp4"
    response = client_visual_off.post(
        "/v1/clip/render",
        json={
            "video_path": str(small_video),
            "segments": [
                {"start_seconds": 0.0, "end_seconds": 1.0, "score": 0.9},
                {"start_seconds": 2.0, "end_seconds": 3.0, "score": 0.8},
            ],
            "output_path": str(output),
        },
    )
    assert response.status_code == 200, response.text
    assert output.is_file() and output.stat().st_size > 0
    assert response.json()["executor_status"]["segment_executor"] == "implemented"


def test_job_pipeline_persists_result_and_product(small_video, tmp_path):
    client = TestClient(
        create_app(Settings(products_dir=str(tmp_path / "products"), job_workers=1))
    )
    created = client.post(
        "/v1/jobs",
        json={
            "video_path": str(small_video),
            "motion_window_count": 4,
            "request_l2": False,
            "output_name": "job-output.mp4",
        },
    )
    assert created.status_code == 202
    job_id = created.json()["job_id"]
    for _ in range(60):
        detail = client.get(f"/v1/jobs/{job_id}").json()
        if detail["status"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.1)
    assert detail["status"] == "completed", detail
    assert detail["result"]["signals"]["candidates"]
    assert detail["result"]["review"]["l2"]["status"] == "skipped"
    assert Path(detail["output_path"]).is_file()
    product = client.get(f"/v1/jobs/{job_id}/product")
    assert product.status_code == 200
    assert product.headers["content-type"].startswith("video/mp4")
