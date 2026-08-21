"""叙事剪辑测试：目标配置加载校验、镜头/对比匹配、模板重排（混剪）、分镜端到端。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.narrative import (
    InterleaveConfig,
    RhythmConfig,
    TargetMatch,
    _apply_rhythm_guard,
    NarrativeConfigError,
    NarrativeTarget,
    NarrativeTargetBook,
    NarrativeTemplate,
    TargetCondition,
    load_narrative_targets,
    match_targets,
    plan_narrative,
)
from app.storyboard import Shot, extract_storyboard, shot_sample_timestamps, split_shots

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NARRATIVE_CONFIG = PROJECT_ROOT / "configs" / "narrative_targets.yaml"
RULE_BOOK = PROJECT_ROOT / "configs" / "style_profiles.yaml"


def make_shot(
    index: int,
    start: float,
    end: float,
    *,
    motion: float = 0.0,
    spike: float = 0.0,
    delta_max: float = 0.0,
) -> Shot:
    return Shot(
        index=index,
        start_seconds=start,
        end_seconds=end,
        duration_seconds=round(end - start, 3),
        sampled_frames=3,
        mean_motion_intensity=motion,
        peak_motion_intensity=motion,
        luminance_spike_ratio=spike,
        luminance_delta_max=delta_max,
    )


def make_book() -> NarrativeTargetBook:
    targets = (
        NarrativeTarget(
            target_id="battle_effects",
            display_name="燃向特效",
            styles=("ran_xiang",),
            description="",
            match="all",
            conditions=(
                TargetCondition("mean_motion_intensity", "gte", 0.30),
                TargetCondition("luminance_spike_ratio", "gte", 0.10),
            ),
        ),
        NarrativeTarget(
            target_id="romance_intimacy",
            display_name="亲密动作",
            styles=("dialogue",),
            description="",
            match="all",
            conditions=(
                TargetCondition("mean_motion_intensity", "lte", 0.18),
                TargetCondition("duration_seconds", "gte", 1.0),
            ),
        ),
        NarrativeTarget(
            target_id="daily_turbulence",
            display_name="波澜起伏",
            styles=("ran_xiang",),
            description="",
            match="pair",
            pair_first=(TargetCondition("mean_motion_intensity", "lte", 0.15),),
            pair_second=(TargetCondition("mean_motion_intensity", "gte", 0.30),),
            pair_gap_seconds=0.5,
        ),
    )
    templates = (
        NarrativeTemplate("linear", "顺叙", "", "chronological"),
        NarrativeTemplate("hook_first", "钩子开场", "", "peak_first"),
        NarrativeTemplate("twist_bridge", "对比开场", "", "contrast_open"),
    )
    return NarrativeTargetBook(
        schema_version="narrative-targets-1.0",
        calibration_status="provisional",
        targets=targets,
        templates=templates,
        interleave=InterleaveConfig(enabled=True, max_pairs=3, min_pair_seconds=1.0),
    )


SHOTS = [
    make_shot(0, 0.0, 2.0, motion=0.05),          # 平静铺垫
    make_shot(1, 2.05, 4.0, motion=0.45, spike=0.3),  # 爆发（战斗/特效）
    make_shot(2, 4.1, 6.0, motion=0.12),          # 低运动长镜（亲密候选）
]


# ---------- 配置加载 ----------


def test_load_real_config():
    book = load_narrative_targets(NARRATIVE_CONFIG)
    assert book.schema_version == "narrative-targets-1.0"
    assert {target.target_id for target in book.targets} == {
        "battle_effects",
        "blood_intensity",
        "romance_intimacy",
        "daily_turbulence",
        "group_action",
    }
    assert len(book.templates) == 4
    assert book.interleave.enabled is True


def test_loader_rejects_bad_schema(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema_version: wrong-1.0\ntargets: []\n", encoding="utf-8")
    with pytest.raises(NarrativeConfigError):
        load_narrative_targets(bad)


def test_loader_rejects_duplicate_target_ids(tmp_path):
    text = NARRATIVE_CONFIG.read_text(encoding="utf-8").replace(
        "id: blood_intensity", "id: battle_effects"
    )
    bad = tmp_path / "dup.yaml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(NarrativeConfigError):
        load_narrative_targets(bad)


def test_loader_rejects_unknown_signal(tmp_path):
    text = NARRATIVE_CONFIG.read_text(encoding="utf-8").replace(
        "signal: mean_motion_intensity", "signal: hallucinated_signal", 1
    )
    bad = tmp_path / "signal.yaml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(NarrativeConfigError):
        load_narrative_targets(bad)


def test_loader_missing_file():
    with pytest.raises(NarrativeConfigError):
        load_narrative_targets("/nonexistent/narrative_targets.yaml")


# ---------- 目标匹配 ----------


def test_match_targets_shot_and_pair():
    book = make_book()
    matches = match_targets(book, SHOTS)
    by_target = {}
    for match in matches:
        by_target.setdefault(match.target_id, []).append(match)
    # battle_effects 命中爆发镜头
    assert by_target["battle_effects"][0].shot_indexes == (1,)
    # romance_intimacy 命中平静镜（shot0 时长 2s / shot2 时长 1.9s）
    romance_shots = {item.shot_indexes for item in by_target["romance_intimacy"]}
    assert romance_shots == {(0,), (2,)}
    # daily_turbulence 命中 (平静 shot0 → 爆发 shot1) 相邻对
    pair = by_target["daily_turbulence"][0]
    assert pair.kind == "pair" and pair.shot_indexes == (0, 1)
    assert pair.score == pytest.approx(0.4, abs=1e-3)
    # 全局按分数降序
    scores = [match.score for match in matches]
    assert scores == sorted(scores, reverse=True)


def test_match_targets_style_filter():
    book = make_book()
    matches = match_targets(book, SHOTS, style_id="ran_xiang")
    assert {match.target_id for match in matches} == {"battle_effects", "daily_turbulence"}


def test_match_pair_respects_gap():
    book = make_book()
    far_shots = [
        make_shot(0, 0.0, 2.0, motion=0.05),
        make_shot(1, 5.0, 7.0, motion=0.45),  # 间隔 3s > pair_gap 0.5s
    ]
    matches = match_targets(book, far_shots)
    assert not [match for match in matches if match.target_id == "daily_turbulence"]


# ---------- 叙事模板重排（混剪） ----------


def test_plan_chronological_keeps_time_order():
    book = make_book()
    matches = match_targets(book, SHOTS)
    segments, notes = plan_narrative(book, matches, "linear", target_duration_seconds=60)
    starts = [segment["start_seconds"] for segment in segments]
    assert starts == sorted(starts)
    assert all(segment["role"] == "body" for segment in segments)


def test_plan_peak_first_puts_best_opening():
    book = make_book()
    matches = match_targets(book, SHOTS, style_id="ran_xiang")
    segments, _ = plan_narrative(book, matches, "hook_first", target_duration_seconds=60)
    assert segments[0]["target_ids"] == ["battle_effects"]
    assert segments[0]["role"] == "opening"
    assert segments[0]["shot_indexes"] == [1]


def test_plan_contrast_open_uses_pair():
    book = make_book()
    matches = match_targets(book, SHOTS, style_id="ran_xiang")
    segments, notes = plan_narrative(book, matches, "twist_bridge", target_duration_seconds=60)
    assert segments[0]["shot_indexes"] == [0, 1]
    assert segments[0]["role"] == "opening"


def test_plan_contrast_open_falls_back_without_pair():
    book = make_book()
    matches = [
        match
        for match in match_targets(book, SHOTS)
        if match.target_id == "battle_effects"
    ]
    segments, notes = plan_narrative(book, matches, "twist_bridge", target_duration_seconds=60)
    assert any("fell back to peak_first" in note for note in notes)
    assert segments[0]["role"] == "opening"


def test_plan_budget_trims_tail():
    book = make_book()
    matches = match_targets(book, SHOTS)
    segments, notes = plan_narrative(book, matches, "linear", target_duration_seconds=3.0)
    total = sum(segment["end_seconds"] - segment["start_seconds"] for segment in segments)
    assert total <= 3.0 + 1e-6
    assert "timeline truncated to target duration" in notes


def test_plan_interleave_adds_note():
    book = make_book()
    matches = match_targets(book, SHOTS)
    segments, notes = plan_narrative(
        book, matches, "linear", target_duration_seconds=60, interleave=True
    )
    assert any("interleave" in note for note in notes)
    assert segments


def test_plan_unknown_template_raises():
    book = make_book()
    with pytest.raises(ValueError):
        plan_narrative(book, match_targets(book, SHOTS), "no_such_template")


def test_plan_empty_matches():
    book = make_book()
    segments, notes = plan_narrative(book, [], "hook_first")
    assert segments == []
    assert notes == ["no target matched; nothing to plan"]


# ---------- 分镜纯逻辑 ----------


def test_split_shots_filters_fragments():
    assert split_shots(4.0, [2.0]) == [(0.0, 2.0), (2.0, 4.0)]
    # ≤0.05s 碎片镜头被过滤
    assert split_shots(4.0, [0.02, 2.0]) == [(0.02, 2.0), (2.0, 4.0)]
    assert split_shots(4.0, [1.0, 1.02]) == [(0.0, 1.0), (1.02, 4.0)]


def test_shot_sample_timestamps_avoid_edges():
    stamps = shot_sample_timestamps(0.0, 2.0, 3)
    assert len(stamps) == 3
    assert stamps[0] >= 0.1 and stamps[-1] <= 1.9
    assert shot_sample_timestamps(0.0, 2.0, 1) == [1.0]


# ---------- 分镜端到端（合成视频：运动镜 + 静止镜） ----------


def test_extract_storyboard_detects_two_shots(small_video):
    info, shots = extract_storyboard(Settings(), str(small_video))
    assert info.duration_seconds == pytest.approx(4.0, abs=0.2)
    assert len(shots) == 2
    motion_shot, static_shot = shots
    assert motion_shot.mean_motion_intensity > static_shot.mean_motion_intensity
    assert motion_shot.start_seconds == 0.0
    assert static_shot.end_seconds == pytest.approx(info.duration_seconds, abs=0.05)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(
        create_app(
            Settings(
                rule_book_path=str(RULE_BOOK),
                narrative_targets_path=str(NARRATIVE_CONFIG),
            )
        )
    )


@pytest.fixture()
def client_no_book() -> TestClient:
    return TestClient(
        create_app(
            Settings(
                rule_book_path=str(RULE_BOOK),
                narrative_targets_path="/nonexistent/narrative_targets.yaml",
            )
        )
    )


def test_ready_includes_narrative(client):
    assert client.get("/ready").json()["features"]["narrative"] is True


def test_get_narrative_summary(client):
    payload = client.get("/v1/narrative").json()
    assert payload["schema_version"] == "narrative-targets-1.0"
    ids = {target["target_id"] for target in payload["targets"]}
    assert "battle_effects" in ids
    assert len(payload["templates"]) == 4


def test_storyboard_extract_route(client, small_video):
    response = client.post(
        "/v1/storyboard/extract", json={"video_path": str(small_video)}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "storyboard-1.0"
    assert payload["shot_count"] == 2


def test_narrative_plan_route(client, small_video):
    response = client.post(
        "/v1/narrative/plan",
        json={
            "video_path": str(small_video),
            "template_id": "hook_first",
            "target_duration_seconds": 15,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "narrative-plan-1.0"
    assert payload["shot_count"] == 2
    assert payload["template_id"] == "hook_first"
    assert isinstance(payload["segments"], list)
    assert payload["total_duration_seconds"] <= 15.0


def test_narrative_routes_degrade_without_book(client_no_book, small_video):
    assert client_no_book.get("/v1/narrative").status_code == 503
    response = client_no_book.post(
        "/v1/storyboard/extract", json={"video_path": str(small_video)}
    )
    assert response.status_code == 200  # 分镜不依赖叙事目标书
    response = client_no_book.post(
        "/v1/narrative/plan", json={"video_path": str(small_video)}
    )
    assert response.status_code == 503


def test_storyboard_extract_bad_path(client):
    response = client.post(
        "/v1/storyboard/extract", json={"video_path": "/nonexistent/video.mp4"}
    )
    assert response.status_code == 404

# ---------- 节奏守卫（镜头时长约束） ----------


def _segment(start: float, end: float, score: float = 0.5, shot: int = 0) -> dict:
    return {
        "start_seconds": start,
        "end_seconds": end,
        "shot_indexes": [shot],
        "target_ids": ["battle_effects"],
        "score": score,
        "role": "body",
    }


RHYTHM = RhythmConfig(min_shot_seconds=1.0, allow_flash_runs=True, flash_run_min_count=2)


def test_rhythm_guard_drops_isolated_short_between_long():
    segments = [
        _segment(0.0, 3.0),
        _segment(10.0, 10.6, shot=1),   # 孤立短镜（与两侧都不相邻）
        _segment(20.0, 25.0, shot=2),
    ]
    guarded, notes = _apply_rhythm_guard(segments, RHYTHM)
    assert [seg["start_seconds"] for seg in guarded] == [0.0, 20.0]
    assert any("rhythm guard dropped 1" in note for note in notes)


def test_rhythm_guard_merges_contiguous_short_into_neighbor():
    segments = [
        _segment(0.0, 3.0),
        _segment(3.0, 3.6, shot=1),   # 与原片相邻 → 并入前段而非剔除
        _segment(20.0, 25.0, shot=2),
    ]
    guarded, notes = _apply_rhythm_guard(segments, RHYTHM)
    assert len(guarded) == 2
    assert guarded[0]["end_seconds"] == 3.6
    assert guarded[0]["shot_indexes"] == [0, 1]
    assert notes == []


def test_rhythm_guard_keeps_flash_run_of_shorts():
    # 连续 3 个短片段 = 快闪串（原片快闪/生成快闪），整体保留
    segments = [
        _segment(0.0, 3.0),
        _segment(5.0, 5.4, shot=1),
        _segment(5.4, 5.8, shot=2),
        _segment(5.8, 6.1, shot=3),
        _segment(20.0, 25.0, shot=4),
    ]
    guarded, notes = _apply_rhythm_guard(segments, RHYTHM)
    assert len(guarded) == 5
    assert notes == []


def test_rhythm_guard_single_short_run_is_not_flash():
    # 单独一个短片段夹在中长之间（即使与一侧相邻）→ 并入而非保留
    segments = [
        _segment(0.0, 3.0),
        _segment(5.0, 5.4, shot=1),   # 与前段不相邻、与后段相邻
        _segment(5.4, 9.0, shot=2),
    ]
    guarded, notes = _apply_rhythm_guard(segments, RHYTHM)
    assert len(guarded) == 2
    assert guarded[1]["start_seconds"] == 5.0
    assert guarded[1]["end_seconds"] == 9.0


# ---------- 钩子关联链 ----------


def _match(target_id: str, start: float, end: float, score: float, shots=(0,)) -> TargetMatch:
    return TargetMatch(
        target_id=target_id,
        kind="shot",
        shot_indexes=shots,
        start_seconds=start,
        end_seconds=end,
        score=score,
    )


def test_hook_chain_prefers_source_adjacent_segment():
    book = make_book()
    matches = [
        _match("battle_effects", 50.0, 55.0, 1.0, shots=(10,)),   # 最高分开场
        _match("battle_effects", 5.0, 8.0, 0.8, shots=(2,)),      # 时间上更靠前
        _match("battle_effects", 55.0, 58.0, 0.6, shots=(11,)),   # 原片上与开场相邻
    ]
    segments, notes = plan_narrative(book, matches, "hook_first", target_duration_seconds=60)
    starts = [seg["start_seconds"] for seg in segments]
    assert starts[0] == 50.0
    assert starts[1] == 55.0  # 关联链选原片相邻片段，而非按时间回到 5s
    assert any("hook chain" in note for note in notes)


def test_loader_rejects_bad_flash_run_count(tmp_path):
    # 直接构造最小非法配置
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        """schema_version: narrative-targets-1.0
calibration:
  status: provisional
targets:
  - id: battle_effects
    styles: [ran_xiang]
    conditions:
      - {signal: mean_motion_intensity, op: gte, value: 0.3}
templates:
  - id: linear
    strategy: chronological
rhythm:
  flash_run_min_count: 1
""",
        encoding="utf-8",
    )
    with pytest.raises(NarrativeConfigError):
        load_narrative_targets(bad)


def test_plan_applies_rhythm_guard_end_to_end():
    book = make_book()
    matches = [
        _match("battle_effects", 0.0, 4.0, 0.9, shots=(0,)),
        _match("group_action", 30.0, 30.5, 0.8, shots=(5,)),  # 孤立短镜
        _match("group_action", 40.0, 45.0, 0.7, shots=(6,)),
    ]
    segments, notes = plan_narrative(book, matches, "hook_first", target_duration_seconds=60)
    for seg in segments:
        assert seg["end_seconds"] - seg["start_seconds"] >= 1.0
    assert any("rhythm guard" in note for note in notes)
