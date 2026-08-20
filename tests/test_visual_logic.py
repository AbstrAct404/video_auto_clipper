"""视觉分析纯逻辑测试（Fake provider，不依赖模型与 ffmpeg）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.visual_service import (
    DEFAULT_VISUAL_CATALOG,
    FakeEmbeddingProvider,
    answer_question,
    resolve_catalog,
)


def _fake_frames(tmp_path: Path, count: int = 2) -> list[Path]:
    paths = []
    for index in range(count):
        frame = tmp_path / f"frame_{index}.pgm"
        frame.write_bytes(b"P5\n2 2\n255\n" + bytes([index * 40] * 4))
        paths.append(frame)
    return paths


def test_answer_question_returns_ranked_candidates(tmp_path):
    provider = FakeEmbeddingProvider()
    item = answer_question(
        provider,
        question_type="shot_size",
        catalog=DEFAULT_VISUAL_CATALOG,
        image_paths=_fake_frames(tmp_path),
        timestamps_seconds=[0.0, 1.0],
    )
    assert item.status in {"answered", "abstained"}
    assert len(item.ranked_candidates) == len(DEFAULT_VISUAL_CATALOG["shot_size"])
    scores = [candidate.score for candidate in item.ranked_candidates]
    assert scores == sorted(scores, reverse=True)
    assert 0.0 <= item.confidence <= 1.0


def test_answer_question_unknown_type_raises(tmp_path):
    provider = FakeEmbeddingProvider()
    with pytest.raises(ValueError, match="unknown visual question type"):
        answer_question(
            provider,
            question_type="not_a_type",
            catalog=DEFAULT_VISUAL_CATALOG,
            image_paths=_fake_frames(tmp_path),
            timestamps_seconds=[0.0, 1.0],
        )


def test_answer_question_requires_two_or_three_frames(tmp_path):
    provider = FakeEmbeddingProvider()
    with pytest.raises(ValueError, match="2-3 aligned frames"):
        answer_question(
            provider,
            question_type="shot_size",
            catalog=DEFAULT_VISUAL_CATALOG,
            image_paths=_fake_frames(tmp_path, count=1),
            timestamps_seconds=[0.0],
        )


def test_fake_provider_is_deterministic():
    provider = FakeEmbeddingProvider()
    first = provider.embed_texts(["a close-up shot"])
    second = provider.embed_texts(["a close-up shot"])
    assert first == second


def test_resolve_catalog_merges_request_overrides():
    merged = resolve_catalog({"custom_group": {"a": "prompt a", "b": "prompt b"}})
    assert "custom_group" in merged
    assert "shot_size" in merged  # 内置目录仍在


def test_resolve_catalog_rejects_oversized_group():
    oversized = {f"v{i}": f"prompt {i}" for i in range(20)}
    with pytest.raises(ValueError, match="at most"):
        resolve_catalog({"too_big": oversized})


def test_resolve_catalog_rejects_too_many_groups():
    too_many = {f"group_{i}": {"a": "prompt a", "b": "prompt b"} for i in range(20)}
    with pytest.raises(ValueError, match="at most"):
        resolve_catalog(too_many)


def test_resolve_catalog_rejects_single_candidate_group():
    with pytest.raises(ValueError, match="at least"):
        resolve_catalog({"bad": {"only": "prompt"}})
