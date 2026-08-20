"""L4 合规门禁 + L2 移交计划测试。"""

from __future__ import annotations

from pathlib import Path

from app.gatekeeper import evaluate_gatekeeper
from app.l2_referral import plan_referrals
from app.style_profiles import load_rule_book

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RULE_BOOK_PATH = PROJECT_ROOT / "configs" / "style_profiles.yaml"


def _rule_book():
    return load_rule_book(RULE_BOOK_PATH)


# ---------- L4 门禁 ----------

def test_gatekeeper_block_hit():
    result = evaluate_gatekeeper(
        _rule_book(), {"violence": "violence_graphic"}
    )
    assert result["verdict"] == "block"
    assert result["block_hits"] == ["violence=violence_graphic"]


def test_gatekeeper_review_hit():
    result = evaluate_gatekeeper(_rule_book(), {"violence": "violence_light"})
    assert result["verdict"] == "review"
    assert result["review_hits"] == ["violence=violence_light"]


def test_gatekeeper_block_beats_review():
    result = evaluate_gatekeeper(
        _rule_book(), {"violence": ["violence_light", "violence_moderate"]}
    )
    assert result["verdict"] == "block"


def test_gatekeeper_pass_and_unknown_dimension():
    result = evaluate_gatekeeper(
        _rule_book(), {"violence": "violence_none", "gambling": "gambling_heavy"}
    )
    assert result["verdict"] == "pass"
    assert any("gambling" in note for note in result["notes"])


def test_gatekeeper_none_label_ignored():
    result = evaluate_gatekeeper(_rule_book(), {"violence": None})
    assert result["verdict"] == "pass"
    assert result["block_hits"] == []


# ---------- L2 移交 ----------

def test_referral_always_trigger():
    """fast_dialogue trigger=always：单命中也移交。"""
    matches = [
        {
            "profile_id": "fast_dialogue",
            "display_name": "快节奏对白",
            "style_score": 0.8,
            "hooks": ["E_suspense_front"],
            "clip_strategy": {},
            "notes": [],
        }
    ]
    referrals = plan_referrals(_rule_book(), matches)
    assert len(referrals) == 1
    assert referrals[0]["trigger"] == "always"
    assert referrals[0]["reason"] == "profile_requires_l2"
    assert referrals[0]["prompt_ref"] == "prompts/style_dialogue.md"


def test_referral_ambiguous_requires_multi_hit():
    """ran_xiang trigger=ambiguous_multi_hit：单命中不移交，双命中移交。"""
    ran_xiang_match = {
        "profile_id": "ran_xiang",
        "display_name": "燃向",
        "style_score": 0.8,
        "hooks": [],
        "clip_strategy": {},
        "notes": [],
    }
    assert plan_referrals(_rule_book(), [ran_xiang_match]) == []

    second = dict(ran_xiang_match, profile_id="fast_dialogue")
    referrals = plan_referrals(_rule_book(), [ran_xiang_match, second])
    ids = {referral["profile_id"] for referral in referrals}
    assert ids == {"ran_xiang", "fast_dialogue"}
    assert all(
        referral["reason"] == "multi_hit_ambiguous" for referral in referrals
    )
