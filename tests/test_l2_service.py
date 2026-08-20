"""L2 OpenAI-compatible adapter tests (network mocked)."""

from __future__ import annotations

import json
from pathlib import Path

from app.config import Settings
from app.l2_service import review_profiles
from app.models import CandidateWindow, SignalValues
from app.style_profiles import load_rule_book


ROOT = Path(__file__).resolve().parent.parent


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return json.dumps(
            {"choices": [{"message": {"content": '{"verdict":"fast_dialogue","confidence":0.9}'}}]}
        ).encode()


def _signals() -> SignalValues:
    return SignalValues(
        shot_cut_rate_per_min=30,
        scene_count=3,
        mean_motion_intensity=0.2,
        peak_motion_intensity=0.3,
        continuous_motion_window_ratio=1.0,
        luminance_spike_ratio=0.02,
        duration_seconds=20,
    )


def test_review_profiles_calls_openai_compatible_endpoint(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data.decode())
        return _Response()

    monkeypatch.setattr("app.l2_service.urlopen", fake_urlopen)
    items = review_profiles(
        Settings(
            l2_enabled=True,
            l2_api_key="test-key",
            l2_base_url="https://example.test/v1/chat/completions",
            l2_model="test-vl",
        ),
        load_rule_book(ROOT / "configs/style_profiles.yaml"),
        signals=_signals(),
        candidates=[
            CandidateWindow(
                start_seconds=1,
                duration_seconds=3,
                score=0.8,
                motion_intensity=0.2,
                peak_motion_intensity=0.3,
                temporal_pattern="continuous",
                luminance_spike_ratio=0.02,
            )
        ],
        profile_ids=["fast_dialogue"],
        image_urls=["https://cdn.example.test/frame.jpg"],
    )
    assert captured["url"].endswith("/chat/completions")
    assert captured["payload"]["model"] == "test-vl"
    assert items[0].result["verdict"] == "fast_dialogue"
