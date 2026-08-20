"""L1 规则卡加载器测试：加载校验 + 求值 + 生产门禁。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models import SignalValues
from app.style_profiles import (
    MIN_SAMPLES_FOR_CALIBRATED,
    RuleBookError,
    load_rule_book,
    match_profiles,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RULE_BOOK_PATH = PROJECT_ROOT / "configs" / "style_profiles.yaml"

VALID_HEADER = """
schema_version: style-profiles-1.0
calibration:
  status: provisional
  sample_set: test
  sample_count: 5
signal_schema:
  version: signals-1.0
  fields:
    - mean_motion_intensity
    - shot_cut_rate_per_min
    - audio_mean_volume_db
"""


def _signals(**overrides) -> SignalValues:
    base = dict(
        shot_cut_rate_per_min=20.0,
        scene_count=4,
        mean_motion_intensity=0.5,
        peak_motion_intensity=0.7,
        continuous_motion_window_ratio=1.0,
        luminance_spike_ratio=0.05,
        duration_seconds=30.0,
    )
    base.update(overrides)
    return SignalValues(**base)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "rule_book.yaml"
    path.write_text(VALID_HEADER + body, encoding="utf-8")
    return path


def test_load_shipped_rule_book():
    rule_book = load_rule_book(RULE_BOOK_PATH)
    assert rule_book.schema_version == "style-profiles-1.0"
    assert rule_book.calibration.status == "provisional"
    ids = [profile.profile_id for profile in rule_book.profiles]
    assert {"ran_xiang", "fast_dialogue", "quality_flag"} <= set(ids)
    assert "violence" in rule_book.gatekeeper


def test_shipped_rule_book_matches_signals():
    """燃向卡阈值与 v2 实测信号匹配：典型燃向信号应命中 ran_xiang。"""
    rule_book = load_rule_book(RULE_BOOK_PATH)
    matches, _ = match_profiles(rule_book, _signals())
    assert "ran_xiang" in {match["profile_id"] for match in matches}


def test_score_features_apply_weighted_signals_outside_hard_conditions():
    rule_book = load_rule_book(RULE_BOOK_PATH)
    quiet, _ = match_profiles(rule_book, _signals(audio_mean_volume_db=-30.0))
    loud, _ = match_profiles(rule_book, _signals(audio_mean_volume_db=-15.0))
    quiet_score = next(item["style_score"] for item in quiet if item["profile_id"] == "ran_xiang")
    loud_score = next(item["style_score"] for item in loud if item["profile_id"] == "ran_xiang")
    assert loud_score > quiet_score


def test_reject_signal_outside_whitelist(tmp_path):
    path = _write(
        tmp_path,
        """
profiles:
  - id: bad
    conditions:
      all:
        - { signal: close_up_ratio, op: gte, value: 0.4 }
""",
    )
    with pytest.raises(RuleBookError, match="whitelist"):
        load_rule_book(path)


def test_reject_condition_nesting_too_deep(tmp_path):
    path = _write(
        tmp_path,
        """
profiles:
  - id: deep
    conditions:
      all:
        - any:
            - all:
                - { signal: mean_motion_intensity, op: gte, value: 0.1 }
""",
    )
    with pytest.raises(RuleBookError, match="nesting"):
        load_rule_book(path)


def test_reject_calibrated_without_enough_samples(tmp_path):
    path = tmp_path / "rule_book.yaml"
    path.write_text(
        VALID_HEADER.replace("status: provisional", "status: calibrated")
        + """
profiles:
  - id: ok
    conditions:
      all:
        - { signal: mean_motion_intensity, op: gte, value: 0.1 }
""",
        encoding="utf-8",
    )
    with pytest.raises(RuleBookError, match=str(MIN_SAMPLES_FOR_CALIBRATED)):
        load_rule_book(path)


def test_reject_duplicate_profile_id(tmp_path):
    path = _write(
        tmp_path,
        """
profiles:
  - id: dup
    conditions:
      all:
        - { signal: mean_motion_intensity, op: gte, value: 0.1 }
  - id: dup
    conditions:
      all:
        - { signal: mean_motion_intensity, op: gte, value: 0.2 }
""",
    )
    with pytest.raises(RuleBookError, match="duplicate"):
        load_rule_book(path)


def test_reject_invalid_op(tmp_path):
    path = _write(
        tmp_path,
        """
profiles:
  - id: bad
    conditions:
      all:
        - { signal: mean_motion_intensity, op: equals, value: 0.1 }
""",
    )
    with pytest.raises(RuleBookError, match="op must be"):
        load_rule_book(path)


def test_missing_signal_treated_as_not_satisfied(tmp_path):
    path = _write(
        tmp_path,
        """
profiles:
  - id: needs_audio
    conditions:
      all:
        - { signal: mean_motion_intensity, op: gte, value: 0.1 }
        - { signal: audio_mean_volume_db, op: gte, value: -30 }
""",
    )
    rule_book = load_rule_book(path)
    # audio_mean_volume_db 缺失（None）→ 整卡不命中并记录 notes
    signals = SignalValues(
        shot_cut_rate_per_min=20.0,
        scene_count=1,
        mean_motion_intensity=0.5,
        peak_motion_intensity=0.6,
        continuous_motion_window_ratio=1.0,
        luminance_spike_ratio=0.0,
        duration_seconds=10.0,
    )
    matches, _ = match_profiles(rule_book, signals)
    assert matches == []


def test_production_requires_calibrated():
    rule_book = load_rule_book(RULE_BOOK_PATH)
    with pytest.raises(RuleBookError, match="calibrated"):
        match_profiles(rule_book, _signals(), production=True)


def test_disabled_profile_skipped():
    """shang_gan 卡 enabled=false，即便信号满足也不应命中。"""
    rule_book = load_rule_book(RULE_BOOK_PATH)
    signals = _signals(mean_motion_intensity=0.1, shot_cut_rate_per_min=5.0,
                       peak_motion_intensity=0.2)
    matches, notes = match_profiles(rule_book, signals)
    assert "shang_gan" not in {match["profile_id"] for match in matches}
    assert any("shang_gan" in note for note in notes)
