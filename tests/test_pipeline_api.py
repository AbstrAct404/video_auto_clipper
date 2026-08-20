"""L1~L4 新路由 API 集成测试（不依赖 ffmpeg，纯契约/规则求值）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RULE_BOOK = PROJECT_ROOT / "configs" / "style_profiles.yaml"

SIGNALS = {
    "shot_cut_rate_per_min": 20.0,
    "scene_count": 4,
    "mean_motion_intensity": 0.5,
    "peak_motion_intensity": 0.7,
    "continuous_motion_window_ratio": 1.0,
    "luminance_spike_ratio": 0.05,
    "duration_seconds": 30.0,
}


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app(Settings(rule_book_path=str(RULE_BOOK))))


@pytest.fixture()
def client_no_rule_book() -> TestClient:
    return TestClient(
        create_app(Settings(rule_book_path="/nonexistent/rule_book.yaml"))
    )


def test_ready_includes_pipeline_features(client):
    features = client.get("/ready").json()["features"]
    assert features["profiles"] is True
    assert features["gatekeeper"] is True
    assert features["clip_planner"] is True


def test_list_profiles(client):
    response = client.get("/v1/profiles")
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "style-profiles-1.0"
    assert payload["calibration"]["status"] == "provisional"
    ids = {profile["profile_id"] for profile in payload["profiles"]}
    assert {"ran_xiang", "fast_dialogue", "quality_flag"} <= ids
    assert "violence" in payload["gatekeeper_dimensions"]


def test_evaluate_profiles_matches_ran_xiang(client):
    response = client.post(
        "/v1/profiles/evaluate", json={"signals": SIGNALS}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "rules-eval-1.0"
    assert payload["rule_book_status"] == "provisional"
    ids = {match["profile_id"] for match in payload["matches"]}
    assert "ran_xiang" in ids
    # ran_xiang 单命中 + ambiguous_multi_hit → 不产生移交
    assert payload["referrals"] == []


def test_evaluate_profiles_production_blocked(client):
    response = client.post(
        "/v1/profiles/evaluate", json={"signals": SIGNALS, "production": True}
    )
    assert response.status_code == 409  # provisional 规则书禁止生产求值


def test_evaluate_profiles_referral_on_multi_hit(client):
    """燃向信号 + 低连续运动比例 → ran_xiang 与 quality_flag 双命中 → 双双移交。"""
    signals = dict(SIGNALS, continuous_motion_window_ratio=0.3)
    response = client.post(
        "/v1/profiles/evaluate", json={"signals": signals}
    )
    assert response.status_code == 200
    payload = response.json()
    ids = {match["profile_id"] for match in payload["matches"]}
    assert {"ran_xiang", "quality_flag"} <= ids
    assert {referral["profile_id"] for referral in payload["referrals"]} == {
        "ran_xiang", "quality_flag",
    }
    assert all(
        referral["reason"] == "multi_hit_ambiguous"
        for referral in payload["referrals"]
    )


def test_gatekeeper_check_block(client):
    response = client.post(
        "/v1/gatekeeper/check", json={"labels": {"violence": "violence_moderate"}}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["verdict"] == "block"
    assert payload["block_hits"] == ["violence=violence_moderate"]


def test_gatekeeper_check_pass_with_unknown_dimension(client):
    response = client.post(
        "/v1/gatekeeper/check", json={"labels": {"drugs": "drug_use"}}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["verdict"] == "pass"
    assert payload["notes"]


def test_clip_plan_route(client):
    response = client.post(
        "/v1/clip/plan",
        json={
            "windows": [
                {"start_seconds": 0.0, "duration_seconds": 8.0, "score": 0.9},
                {"start_seconds": 20.0, "duration_seconds": 9.0, "score": 0.7},
            ],
            "target_duration_seconds": 15.0,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "clip-plan-1.0"
    assert payload["total_duration_seconds"] == pytest.approx(15.0)
    assert payload["executor_status"]["segment_executor"] == "implemented"


def test_l2_review_requires_enabled_provider(client):
    response = client.post(
        "/v1/l2/review",
        json={
            "signals": SIGNALS,
            "profile_ids": ["fast_dialogue"],
            "candidates": [
                {
                    "start_seconds": 0,
                    "duration_seconds": 3,
                    "score": 0.8,
                    "motion_intensity": 0.2,
                    "peak_motion_intensity": 0.3,
                    "temporal_pattern": "continuous",
                    "luminance_spike_ratio": 0.02,
                }
            ],
        },
    )
    assert response.status_code == 503


def test_routes_503_when_rule_book_missing(client_no_rule_book):
    assert client_no_rule_book.get("/ready").json()["features"]["profiles"] is False
    assert client_no_rule_book.get("/v1/profiles").status_code == 503
    assert (
        client_no_rule_book.post(
            "/v1/gatekeeper/check", json={"labels": {}}
        ).status_code
        == 503
    )
