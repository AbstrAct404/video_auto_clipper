"""L2 移交计划：决定哪些命中需要云端 LLM 复核（只规划，不调用 LLM）。

触发规则来自规则卡 l2_fallback.trigger：
- always：该卡命中即移交（一期信号不足的卡，如快节奏对白）；
- ambiguous_multi_hit：≥2 张卡同时命中时才移交最模糊的情况。
移交内容受 max_windows 限制，控制云端成本。
"""

from __future__ import annotations

from typing import Any

from .style_profiles import RuleBook, StyleProfile


def plan_referrals(
    rule_book: RuleBook, matches: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """输入 match_profiles 的命中列表，输出 L2 移交计划数组。"""
    profiles_by_id: dict[str, StyleProfile] = {
        profile.profile_id: profile for profile in rule_book.profiles
    }
    multi_hit = len(matches) >= 2
    referrals: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        profile = profiles_by_id.get(match["profile_id"])
        if profile is None or profile.l2_fallback is None:
            continue
        fallback = profile.l2_fallback
        if fallback.trigger == "always" or (
            fallback.trigger == "ambiguous_multi_hit" and multi_hit
        ):
            referrals.append(
                {
                    "profile_id": profile.profile_id,
                    "prompt_ref": fallback.prompt_ref,
                    "trigger": fallback.trigger,
                    "max_windows": fallback.max_windows,
                    "reason": (
                        "multi_hit_ambiguous" if multi_hit else "profile_requires_l2"
                    ),
                    "rank": index,
                }
            )
    return referrals
