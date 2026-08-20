"""平台画像与降重路由 API 集成测试（ffmpeg fixture 端到端）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROFILES = PROJECT_ROOT / "configs" / "platform_profiles.yaml"
RULE_BOOK = PROJECT_ROOT / "configs" / "style_profiles.yaml"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(
        create_app(
            Settings(rule_book_path=str(RULE_BOOK), platform_profiles_path=str(PROFILES))
        )
    )


def test_ready_includes_platform_features(client):
    features = client.get("/ready").json()["features"]
    assert features["platforms"] is True
    assert features["dedup"] is True


def test_list_platforms_taxonomy_and_dedup(client):
    response = client.get("/v1/platforms")
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "platform-profiles-1.0"
    ids = {category["id"] for category in payload["unified_categories"]}
    assert "drama_clip" in ids
    # 短剧剧情切片在 B 站映射到官方分区 tid=183（影视剪辑）
    drama = next(
        category
        for category in payload["unified_categories"]
        if category["id"] == "drama_clip"
    )
    assert drama["platforms"]["bilibili"]["tid"] == 183
    assert payload["bilibili_tid_reference"]["181"] == "影视"
    # 判重规则覆盖用户要求的平台
    assert {"douyin", "kuaishou", "xiaohongshu", "bilibili", "instagram", "x"} <= set(
        payload["dedup_platforms"]
    )
    assert payload["dedup_platforms"]["xiaohongshu"]["window_days"] == 90


def test_dedup_analyze_fixture_video(client, small_video):
    response = client.post(
        "/v1/dedup/analyze", json={"video_path": str(small_video), "max_frames": 8}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "dedup-1.0"
    assert len(payload["md5"]) == 32
    assert payload["frame_count"] >= 4
    assert len(payload["fingerprints"]) == payload["frame_count"]
    assert payload["verdict"] in {"pass", "review", "block"}
    assert set(payload["corner_static_ratios"]) == {
        "top_left", "top_right", "bottom_left", "bottom_right",
    }


def test_dedup_compare_self_is_block(client, small_video):
    """与自身比对相似度应 ≈1 → block（近似完全搬运）。"""
    response = client.post(
        "/v1/dedup/compare",
        json={
            "video_path": str(small_video),
            "reference_paths": [str(small_video)],
            "max_frames": 8,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["max_similarity"] == pytest.approx(1.0, abs=1e-3)
    assert payload["verdict"] == "block"
    assert "near_duplicate" in payload["flags"]


def test_dedup_compare_missing_reference_404(client, small_video):
    response = client.post(
        "/v1/dedup/compare",
        json={
            "video_path": str(small_video),
            "reference_paths": ["/nonexistent.mp4"],
        },
    )
    assert response.status_code == 404


def test_routes_503_when_profiles_missing(small_video):
    client = TestClient(
        create_app(
            Settings(
                rule_book_path=str(RULE_BOOK),
                platform_profiles_path="/nonexistent/platform_profiles.yaml",
            )
        )
    )
    assert client.get("/ready").json()["features"]["platforms"] is False
    assert client.get("/v1/platforms").status_code == 503
    assert (
        client.post(
            "/v1/dedup/analyze", json={"video_path": str(small_video)}
        ).status_code
        == 503
    )
