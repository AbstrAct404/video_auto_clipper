"""L4 合规门禁：确定性查表（block/review 两级），不做模糊语义判断。

标签来源是 tagging 14 维合规输出或 LLM 打标；本模块只按规则书
gatekeeper 配置查表，保证可审计（对齐 tagging Gatekeeper 思路）。
"""

from __future__ import annotations

from typing import Any

from .style_profiles import RuleBook


def evaluate_gatekeeper(
    rule_book: RuleBook, labels: dict[str, str | list[str] | None]
) -> dict[str, Any]:
    """输入维度→标签（如 {"violence": "violence_light"}），输出确定性结论。

    verdict 三级：block（自动拦截）> review（转人审）> pass。
    未知维度/缺失标签视为 pass 并记录 notes，绝不因配置缺失误拦。
    """
    config = rule_book.gatekeeper
    block_hits: list[str] = []
    review_hits: list[str] = []
    notes: list[str] = []

    for dimension, raw_label in labels.items():
        if raw_label is None:
            continue
        dimension_config = config.get(dimension)
        if not isinstance(dimension_config, dict):
            notes.append(f"dimension '{dimension}' not configured in gatekeeper; ignored")
            continue
        values = [raw_label] if isinstance(raw_label, str) else list(raw_label)
        block_set = set(dimension_config.get("block") or [])
        review_set = set(dimension_config.get("review") or [])
        for value in values:
            if value in block_set:
                block_hits.append(f"{dimension}={value}")
            elif value in review_set:
                review_hits.append(f"{dimension}={value}")

    if block_hits:
        verdict = "block"
    elif review_hits:
        verdict = "review"
    else:
        verdict = "pass"
    return {
        "verdict": verdict,
        "block_hits": block_hits,
        "review_hits": review_hits,
        "notes": notes,
    }
