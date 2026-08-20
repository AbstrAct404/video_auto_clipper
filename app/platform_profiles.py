"""平台画像加载器：视频类型划分（taxonomy）+ 重复率判定规则（dedup_rules）。

配置文件 configs/platform_profiles.yaml（schema: platform-profiles-1.0），
内容来自各大平台公开规则调研（见 docs/platform-taxonomy-and-dedup.md）。
加载时做结构校验：缺关键块直接报错，宁可启动降级也不静默缺项。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SUPPORTED_SCHEMA_VERSION = "platform-profiles-1.0"
ALLOWED_STATUS = {"provisional", "validated"}
REQUIRED_THRESHOLD_KEYS = {
    "similarity_review",
    "similarity_block",
    "corner_static_watermark",
    "black_bar_ratio_review",
}


class PlatformProfilesError(ValueError):
    """平台画像配置校验失败。"""


@dataclass(frozen=True)
class PlatformProfiles:
    schema_version: str
    status: str
    updated_at: str | None
    sources: tuple[str, ...]
    unified_categories: tuple[dict[str, Any], ...]
    platform_specs: dict[str, Any]
    bilibili_tid_reference: dict[int, str]
    dedup_common_stages: tuple[dict[str, Any], ...]
    dedup_platforms: dict[str, Any]
    thresholds: dict[str, float]
    source_path: str | None = None


def _fail(message: str) -> PlatformProfilesError:
    return PlatformProfilesError(message)


def load_platform_profiles(path: str | Path) -> PlatformProfiles:
    source = Path(path)
    if not source.is_file():
        raise PlatformProfilesError(f"platform profiles not found: {source}")
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise _fail("platform profiles root must be a mapping")

    version = data.get("schema_version")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise _fail(
            f"unsupported schema_version {version!r}; expected {SUPPORTED_SCHEMA_VERSION}"
        )
    status = data.get("status")
    if status not in ALLOWED_STATUS:
        raise _fail(f"status must be one of {sorted(ALLOWED_STATUS)}, got {status!r}")

    taxonomy = data.get("taxonomy") or {}
    if not isinstance(taxonomy, dict):
        raise _fail("taxonomy must be a mapping")
    categories = taxonomy.get("unified_categories") or []
    if not categories:
        raise _fail("taxonomy.unified_categories must not be empty")
    seen: set[str] = set()
    for index, category in enumerate(categories):
        if not isinstance(category, dict):
            raise _fail(f"unified_categories[{index}]: must be a mapping")
        category_id = category.get("id")
        if not category_id:
            raise _fail(f"unified_categories[{index}]: missing id")
        if category_id in seen:
            raise _fail(f"duplicate unified category id: {category_id}")
        if not category.get("platforms"):
            raise _fail(f"unified_categories[{index}] '{category_id}': missing platforms")
        seen.add(category_id)

    specs = taxonomy.get("platform_specs") or {}
    if not isinstance(specs, dict) or not specs:
        raise _fail("taxonomy.platform_specs must not be empty")
    for platform, spec in specs.items():
        if not isinstance(spec, dict) or not {"aspect", "max_seconds", "recommend_seconds"} <= set(spec):
            raise _fail(f"platform_specs.{platform}: requires aspect/max_seconds/recommend_seconds")
        if float(spec["max_seconds"]) <= 0 or float(spec["recommend_seconds"]) <= 0:
            raise _fail(f"platform_specs.{platform}: durations must be positive")

    dedup = data.get("dedup_rules") or {}
    if not isinstance(dedup, dict):
        raise _fail("dedup_rules must be a mapping")
    stages = dedup.get("common_stages") or []
    dedup_platforms = dedup.get("platforms") or {}
    if not stages or not dedup_platforms:
        raise _fail("dedup_rules requires both common_stages and platforms")
    thresholds = {
        key: float(value) for key, value in (dedup.get("thresholds") or {}).items()
    }
    missing = REQUIRED_THRESHOLD_KEYS - set(thresholds)
    if missing:
        raise _fail(f"dedup_rules.thresholds missing keys: {sorted(missing)}")
    if thresholds["similarity_review"] >= thresholds["similarity_block"]:
        raise _fail("similarity_review must be lower than similarity_block")
    if any(value < 0 or value > 1 for value in thresholds.values()):
        raise _fail("dedup_rules.thresholds values must be within [0, 1]")

    tid_reference = {
        int(key): str(name)
        for key, name in (taxonomy.get("bilibili_tid_reference") or {}).items()
    }
    return PlatformProfiles(
        schema_version=version,
        status=status,
        updated_at=data.get("updated_at"),
        sources=tuple(data.get("sources") or []),
        unified_categories=tuple(categories),
        platform_specs=dict(taxonomy.get("platform_specs") or {}),
        bilibili_tid_reference=tid_reference,
        dedup_common_stages=tuple(stages),
        dedup_platforms=dict(dedup_platforms),
        thresholds=thresholds,
        source_path=str(source),
    )
