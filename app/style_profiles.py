"""风格画像规则库加载器（L1）：YAML 解析 + 严格校验 + 信号求值。

设计依据 docs/style-profiles-yaml-design.md：
- 条件信号必须在 signal_schema.fields 白名单内，未知字段直接报错；
- 条件嵌套最多两层，防止规则卡变成不可解释的黑盒；
- 信号缺失（None）时条件视为不满足并记录 notes，绝不默认放行；
- status != calibrated 的规则书只允许试运行（production=True 时拒绝求值）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import SignalValues

SUPPORTED_SCHEMA_VERSION = "style-profiles-1.0"
ALLOWED_STATUS = {"provisional", "calibrated", "deprecated"}
ALLOWED_OPS = {"gte", "gt", "lte", "lt", "between"}
MAX_CONDITION_DEPTH = 2
# 加载器硬规则：样本数不足时禁止声称已标定
MIN_SAMPLES_FOR_CALIBRATED = 30


class RuleBookError(ValueError):
    """规则书格式/语义校验失败。"""


@dataclass(frozen=True)
class Condition:
    signal: str
    op: str
    value: float | None = None
    low: float | None = None
    high: float | None = None


@dataclass
class ConditionBlock:
    """all=AND / any=OR，可嵌套但深度受限。"""

    all: list[Any] = field(default_factory=list)  # Condition | ConditionBlock
    any: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class L2Fallback:
    prompt_ref: str
    trigger: str  # always | ambiguous_multi_hit
    max_windows: int


@dataclass(frozen=True)
class ScoreFeature:
    """A calibrated feature used for ranking after a profile has matched."""

    direction: str  # higher | lower
    anchor: float
    scale: float


@dataclass(frozen=True)
class StyleProfile:
    profile_id: str
    display_name: str
    enabled: bool
    conditions: ConditionBlock
    score_weights: dict[str, float]
    score_features: dict[str, ScoreFeature]
    hooks: list[str]
    l2_fallback: L2Fallback | None
    clip_strategy: dict[str, Any]


@dataclass(frozen=True)
class Calibration:
    status: str
    sample_set: str
    sample_count: int
    calibrated_at: str | None
    next_review_at: str | None


@dataclass(frozen=True)
class RuleBook:
    schema_version: str
    calibration: Calibration
    signal_fields: frozenset[str]
    profiles: tuple[StyleProfile, ...]
    gatekeeper: dict[str, Any]
    source_path: str | None = None


def _fail(message: str) -> RuleBookError:
    return RuleBookError(message)


def _parse_condition(raw: dict[str, Any], where: str) -> Condition:
    allowed_keys = {"signal", "op", "value", "low", "high"}
    extra = set(raw) - allowed_keys
    if extra:
        raise _fail(f"{where}: unknown condition keys {sorted(extra)}")
    op = raw.get("op")
    if op not in ALLOWED_OPS:
        raise _fail(f"{where}: op must be one of {sorted(ALLOWED_OPS)}, got {op!r}")
    if op == "between":
        low, high = raw.get("low"), raw.get("high")
        if low is None or high is None or low > high:
            raise _fail(f"{where}: between requires low <= high")
        return Condition(signal=raw["signal"], op=op, low=float(low), high=float(high))
    if raw.get("value") is None:
        raise _fail(f"{where}: op {op!r} requires numeric value")
    return Condition(signal=raw["signal"], op=op, value=float(raw["value"]))


def _parse_block(raw: dict[str, Any], fields: frozenset[str], depth: int, where: str) -> ConditionBlock:
    if depth > MAX_CONDITION_DEPTH:
        raise _fail(f"{where}: condition nesting exceeds {MAX_CONDITION_DEPTH} levels")
    allowed_keys = {"all", "any"}
    extra = set(raw) - allowed_keys
    if extra:
        raise _fail(f"{where}: unknown condition block keys {sorted(extra)}")
    block = ConditionBlock()
    for key, bucket in (("all", block.all), ("any", block.any)):
        for index, item in enumerate(raw.get(key, [])):
            item_where = f"{where}.{key}[{index}]"
            if not isinstance(item, dict):
                raise _fail(f"{item_where}: condition must be a mapping")
            if "all" in item or "any" in item:
                bucket.append(_parse_block(item, fields, depth + 1, item_where))
                continue
            if "signal" not in item:
                raise _fail(f"{item_where}: missing 'signal'")
            if item["signal"] not in fields:
                raise _fail(
                    f"{item_where}: signal '{item['signal']}' not in signal_schema.fields whitelist"
                )
            bucket.append(_parse_condition(item, item_where))
    if not block.all and not block.any:
        raise _fail(f"{where}: conditions must contain at least one all/any entry")
    return block


def _parse_profile(raw: dict[str, Any], fields: frozenset[str], index: int) -> StyleProfile:
    where = f"profiles[{index}]"
    if not isinstance(raw, dict):
        raise _fail(f"{where}: profile must be a mapping")
    profile_id = raw.get("id")
    if not profile_id:
        raise _fail(f"{where}: missing id")
    conditions_raw = raw.get("conditions")
    if not isinstance(conditions_raw, dict):
        raise _fail(f"{where}: missing conditions block")
    l2_raw = raw.get("l2_fallback")
    l2 = None
    if l2_raw is not None:
        if not isinstance(l2_raw, dict):
            raise _fail(f"{where}.l2_fallback: must be a mapping")
        trigger = l2_raw.get("trigger")
        if trigger not in {"always", "ambiguous_multi_hit"}:
            raise _fail(f"{where}.l2_fallback: invalid trigger {trigger!r}")
        max_windows = l2_raw.get("max_windows", 3)
        if not isinstance(max_windows, int) or isinstance(max_windows, bool) or max_windows < 1:
            raise _fail(f"{where}.l2_fallback.max_windows must be a positive integer")
        prompt_ref = l2_raw.get("prompt_ref", "")
        if not isinstance(prompt_ref, str) or not prompt_ref.strip():
            raise _fail(f"{where}.l2_fallback.prompt_ref must be a non-empty string")
        l2 = L2Fallback(
            prompt_ref=prompt_ref,
            trigger=trigger,
            max_windows=max_windows,
        )
    weights_raw = raw.get("score_weights") or {}
    features_raw = raw.get("score_features") or {}
    if not isinstance(weights_raw, dict) or not isinstance(features_raw, dict):
        raise _fail(f"{where}: score_weights and score_features must be mappings")
    score_weights = {key: float(value) for key, value in weights_raw.items()}
    if any(key not in fields for key in score_weights):
        raise _fail(f"{where}.score_weights contains a signal outside the whitelist")
    if any(value < 0 for value in score_weights.values()):
        raise _fail(f"{where}.score_weights must be non-negative")
    if set(score_weights) != set(features_raw):
        raise _fail(f"{where}: score_weights and score_features must name the same signals")
    score_features: dict[str, ScoreFeature] = {}
    for signal, feature_raw in features_raw.items():
        if signal not in fields or not isinstance(feature_raw, dict):
            raise _fail(f"{where}.score_features.{signal}: invalid feature")
        direction = feature_raw.get("direction")
        if direction not in {"higher", "lower"}:
            raise _fail(f"{where}.score_features.{signal}.direction must be higher or lower")
        try:
            anchor = float(feature_raw["anchor"])
            scale = float(feature_raw["scale"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _fail(f"{where}.score_features.{signal}: anchor and scale are numeric") from exc
        if scale <= 0:
            raise _fail(f"{where}.score_features.{signal}.scale must be positive")
        score_features[signal] = ScoreFeature(direction, anchor, scale)
    return StyleProfile(
        profile_id=profile_id,
        display_name=raw.get("display_name", profile_id),
        enabled=bool(raw.get("enabled", True)),
        conditions=_parse_block(conditions_raw, fields, depth=1, where=f"{where}.conditions"),
        score_weights=score_weights,
        score_features=score_features,
        hooks=list(raw.get("hooks") or []),
        l2_fallback=l2,
        clip_strategy=dict(raw.get("clip_strategy") or {}),
    )


def load_rule_book(path: str | Path) -> RuleBook:
    """加载并严格校验规则书；任何格式问题抛 RuleBookError（宁可启动失败）。"""
    source = Path(path)
    if not source.is_file():
        raise RuleBookError(f"rule book not found: {source}")
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise _fail("rule book root must be a mapping")

    version = data.get("schema_version")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise _fail(
            f"unsupported schema_version {version!r}; expected {SUPPORTED_SCHEMA_VERSION}"
        )

    cal_raw = data.get("calibration") or {}
    status = cal_raw.get("status")
    if status not in ALLOWED_STATUS:
        raise _fail(f"calibration.status must be one of {sorted(ALLOWED_STATUS)}")
    sample_count = int(cal_raw.get("sample_count", 0))
    if status == "calibrated" and sample_count < MIN_SAMPLES_FOR_CALIBRATED:
        raise _fail(
            f"status=calibrated requires sample_count >= {MIN_SAMPLES_FOR_CALIBRATED}, "
            f"got {sample_count}"
        )
    calibration = Calibration(
        status=status,
        sample_set=str(cal_raw.get("sample_set", "")),
        sample_count=sample_count,
        calibrated_at=cal_raw.get("calibrated_at"),
        next_review_at=cal_raw.get("next_review_at"),
    )

    schema_raw = data.get("signal_schema") or {}
    fields = frozenset(schema_raw.get("fields") or [])
    if not fields:
        raise _fail("signal_schema.fields must not be empty")

    profiles_raw = data.get("profiles") or []
    profiles = tuple(
        _parse_profile(raw, fields, index) for index, raw in enumerate(profiles_raw)
    )
    seen: set[str] = set()
    for profile in profiles:
        if profile.profile_id in seen:
            raise _fail(f"duplicate profile id: {profile.profile_id}")
        seen.add(profile.profile_id)

    return RuleBook(
        schema_version=version,
        calibration=calibration,
        signal_fields=fields,
        profiles=profiles,
        gatekeeper=dict(data.get("gatekeeper") or {}),
        source_path=str(source),
    )


# ---------- 求值 ----------

def _check_condition(condition: Condition, signals: SignalValues, notes: list[str]) -> bool:
    value = getattr(signals, condition.signal, None)
    if value is None:
        notes.append(f"signal '{condition.signal}' missing; condition treated as not satisfied")
        return False
    op = condition.op
    if op == "gte":
        return value >= condition.value
    if op == "gt":
        return value > condition.value
    if op == "lte":
        return value <= condition.value
    if op == "lt":
        return value < condition.value
    return condition.low <= value <= condition.high


def evaluate_block(block: ConditionBlock, signals: SignalValues, notes: list[str]) -> bool:
    results_all = [_evaluate_item(item, signals, notes) for item in block.all]
    results_any = [_evaluate_item(item, signals, notes) for item in block.any]
    ok = True
    if results_all:
        ok = ok and all(results_all)
    if results_any:
        ok = ok and any(results_any)
    return ok


def _evaluate_item(item: Any, signals: SignalValues, notes: list[str]) -> bool:
    if isinstance(item, Condition):
        return _check_condition(item, signals, notes)
    return evaluate_block(item, signals, notes)


def _style_score(profile: StyleProfile, signals: SignalValues) -> float:
    """provisional 软评分：按权重信号的超阈幅度（相对阈值）归一到 0~1。

    正式标定后将改为基于样本分布的 min-max 归一（见设计文档 §2.3）。
    """
    if not profile.score_weights:
        return 1.0
    contributions: list[tuple[float, float]] = []
    for signal, weight in profile.score_weights.items():
        value = getattr(signals, signal, None)
        if value is None:
            continue
        feature = profile.score_features[signal]
        margin = (
            (value - feature.anchor) / feature.scale
            if feature.direction == "higher"
            else (feature.anchor - value) / feature.scale
        )
        contributions.append((weight, max(0.0, min(1.0, margin))))
    if not contributions:
        return 1.0
    total_weight = sum(weight for weight, _ in contributions)
    if total_weight <= 0:
        return 1.0
    return round(sum(weight * score for weight, score in contributions) / total_weight, 4)


def _iter_conditions(block: ConditionBlock):
    for item in [*block.all, *block.any]:
        if isinstance(item, Condition):
            yield item
        else:
            yield from _iter_conditions(item)


def match_profiles(rule_book: RuleBook, signals: SignalValues, *, production: bool = False):
    """返回 (matches, notes)。production=True 且规则书未标定时拒绝求值。"""
    if production and rule_book.calibration.status != "calibrated":
        raise RuleBookError(
            "production evaluation requires calibration.status == 'calibrated'; "
            f"current status is '{rule_book.calibration.status}'"
        )
    notes: list[str] = [f"rule book status: {rule_book.calibration.status}"]
    matches: list[dict[str, Any]] = []
    for profile in rule_book.profiles:
        if not profile.enabled:
            notes.append(f"profile '{profile.profile_id}' disabled; skipped")
            continue
        profile_notes: list[str] = []
        if evaluate_block(profile.conditions, signals, profile_notes):
            matches.append(
                {
                    "profile_id": profile.profile_id,
                    "display_name": profile.display_name,
                    "style_score": _style_score(profile, signals),
                    "hooks": list(profile.hooks),
                    "clip_strategy": dict(profile.clip_strategy),
                    "notes": profile_notes,
                }
            )
    return matches, notes
