"""叙事剪辑：风格→画面目标匹配 + 叙事模板重排（允许混剪打乱时间顺序）。

配合 storyboard.py 的镜头级信号工作：
- match_targets：按规则卡的风格归属过滤目标，逐镜（或相邻镜头对）求值；
- plan_narrative：按叙事模板把命中镜头重排为成片时间轴
  （chronological / peak_first / contrast_open / 人物-行动交错）。
本地只做像素级粗筛；人物、亲密动作等语义由 L2（prompts/narrative_board.md）确认。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .storyboard import Shot

ALLOWED_OPS = {"gte", "gt", "lte", "lt"}
ALLOWED_STRATEGIES = {"chronological", "peak_first", "contrast_open"}
ALLOWED_MATCH = {"all", "any", "pair"}
SHOT_SIGNAL_FIELDS = frozenset(
    {
        "duration_seconds",
        "mean_motion_intensity",
        "peak_motion_intensity",
        "luminance_spike_ratio",
        "luminance_delta_max",
    }
)


class NarrativeConfigError(ValueError):
    """叙事目标配置加载/校验失败。"""


@dataclass(frozen=True)
class TargetCondition:
    signal: str
    op: str
    value: float


@dataclass(frozen=True)
class NarrativeTarget:
    target_id: str
    display_name: str
    styles: tuple[str, ...]
    description: str
    match: str  # all | any | pair
    conditions: tuple[TargetCondition, ...] = ()
    pair_first: tuple[TargetCondition, ...] = ()
    pair_second: tuple[TargetCondition, ...] = ()
    pair_gap_seconds: float = 0.5
    l2_hint: str | None = None


@dataclass(frozen=True)
class NarrativeTemplate:
    template_id: str
    display_name: str
    description: str
    strategy: str


@dataclass(frozen=True)
class InterleaveConfig:
    enabled: bool
    max_pairs: int
    min_pair_seconds: float


@dataclass(frozen=True)
class NarrativeTargetBook:
    schema_version: str
    calibration_status: str
    targets: tuple[NarrativeTarget, ...]
    templates: tuple[NarrativeTemplate, ...]
    interleave: InterleaveConfig
    source_path: str | None = None


@dataclass(frozen=True)
class TargetMatch:
    target_id: str
    kind: str  # shot | pair
    shot_indexes: tuple[int, ...]
    start_seconds: float
    end_seconds: float
    score: float


def _fail(message: str) -> NarrativeConfigError:
    return NarrativeConfigError(message)


def _parse_condition(raw: Any, where: str) -> TargetCondition:
    if not isinstance(raw, dict):
        raise _fail(f"{where}: condition must be a mapping")
    extra = set(raw) - {"signal", "op", "value"}
    if extra:
        raise _fail(f"{where}: unknown condition keys {sorted(extra)}")
    signal, op, value = raw.get("signal"), raw.get("op"), raw.get("value")
    if signal not in SHOT_SIGNAL_FIELDS:
        raise _fail(f"{where}: signal must be one of {sorted(SHOT_SIGNAL_FIELDS)}, got {signal!r}")
    if op not in ALLOWED_OPS:
        raise _fail(f"{where}: op must be one of {sorted(ALLOWED_OPS)}, got {op!r}")
    if not isinstance(value, (int, float)):
        raise _fail(f"{where}: value must be numeric")
    return TargetCondition(signal=signal, op=op, value=float(value))


def _parse_conditions(raw: Any, where: str) -> tuple[TargetCondition, ...]:
    if not isinstance(raw, list) or not raw:
        raise _fail(f"{where}: conditions must be a non-empty list")
    return tuple(_parse_condition(item, f"{where}[{index}]") for index, item in enumerate(raw))


def _parse_target(raw: Any, index: int) -> NarrativeTarget:
    where = f"targets[{index}]"
    if not isinstance(raw, dict):
        raise _fail(f"{where}: target must be a mapping")
    target_id = raw.get("id")
    match = raw.get("match", "all")
    if not target_id or not isinstance(target_id, str):
        raise _fail(f"{where}: id is required")
    if match not in ALLOWED_MATCH:
        raise _fail(f"{where}: match must be one of {sorted(ALLOWED_MATCH)}, got {match!r}")
    styles = raw.get("styles") or []
    if not isinstance(styles, list) or not all(isinstance(item, str) for item in styles):
        raise _fail(f"{where}: styles must be a list of style profile ids")
    if match == "pair":
        pair = raw.get("pair_conditions")
        if not isinstance(pair, dict) or "first" not in pair or "second" not in pair:
            raise _fail(f"{where}: pair targets require pair_conditions.first/second")
        return NarrativeTarget(
            target_id=target_id,
            display_name=raw.get("display_name", target_id),
            styles=tuple(styles),
            description=raw.get("description", ""),
            match=match,
            pair_first=_parse_conditions(pair["first"], f"{where}.pair_conditions.first"),
            pair_second=_parse_conditions(pair["second"], f"{where}.pair_conditions.second"),
            pair_gap_seconds=float(raw.get("pair_gap_seconds", 0.5)),
            l2_hint=raw.get("l2_hint"),
        )
    return NarrativeTarget(
        target_id=target_id,
        display_name=raw.get("display_name", target_id),
        styles=tuple(styles),
        description=raw.get("description", ""),
        match=match,
        conditions=_parse_conditions(raw.get("conditions"), f"{where}.conditions"),
        l2_hint=raw.get("l2_hint"),
    )


def load_narrative_targets(path: str | Path) -> NarrativeTargetBook:
    """加载并严格校验叙事目标配置；任何格式问题抛 NarrativeConfigError。"""
    source = Path(path)
    if not source.is_file():
        raise _fail(f"narrative targets not found: {source}")
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise _fail(f"invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise _fail("top level must be a mapping")
    if raw.get("schema_version") != "narrative-targets-1.0":
        raise _fail(f"unsupported schema_version: {raw.get('schema_version')!r}")
    calibration = raw.get("calibration") or {}
    status = calibration.get("status")
    if status not in {"provisional", "calibrated", "deprecated"}:
        raise _fail(f"calibration.status invalid: {status!r}")
    targets_raw = raw.get("targets")
    if not isinstance(targets_raw, list) or not targets_raw:
        raise _fail("targets must be a non-empty list")
    targets = tuple(_parse_target(item, index) for index, item in enumerate(targets_raw))
    seen_ids = {target.target_id for target in targets}
    if len(seen_ids) != len(targets):
        raise _fail("duplicate target ids")
    templates_raw = raw.get("templates")
    if not isinstance(templates_raw, list) or not templates_raw:
        raise _fail("templates must be a non-empty list")
    templates: list[NarrativeTemplate] = []
    for index, item in enumerate(templates_raw):
        where = f"templates[{index}]"
        if not isinstance(item, dict) or not item.get("id"):
            raise _fail(f"{where}: id is required")
        strategy = item.get("strategy")
        if strategy not in ALLOWED_STRATEGIES:
            raise _fail(f"{where}: strategy must be one of {sorted(ALLOWED_STRATEGIES)}")
        templates.append(
            NarrativeTemplate(
                template_id=item["id"],
                display_name=item.get("display_name", item["id"]),
                description=item.get("description", ""),
                strategy=strategy,
            )
        )
    if len({template.template_id for template in templates}) != len(templates):
        raise _fail("duplicate template ids")
    inter_raw = raw.get("interleave") or {}
    interleave = InterleaveConfig(
        enabled=bool(inter_raw.get("enabled", False)),
        max_pairs=int(inter_raw.get("max_pairs", 3)),
        min_pair_seconds=float(inter_raw.get("min_pair_seconds", 1.0)),
    )
    return NarrativeTargetBook(
        schema_version=raw["schema_version"],
        calibration_status=status,
        targets=targets,
        templates=tuple(templates),
        interleave=interleave,
        source_path=str(source),
    )


# ---------- 目标匹配 ----------


def _eval_condition(condition: TargetCondition, shot: Shot) -> bool:
    value = getattr(shot, condition.signal)
    if condition.op == "gte":
        return value >= condition.value
    if condition.op == "gt":
        return value > condition.value
    if condition.op == "lte":
        return value <= condition.value
    return value < condition.value


def _eval_conditions(
    conditions: tuple[TargetCondition, ...], shot: Shot, match: str
) -> bool:
    results = [_eval_condition(condition, shot) for condition in conditions]
    return all(results) if match == "all" else any(results)


def _shot_score(target: NarrativeTarget, shot: Shot) -> float:
    """镜头命中分数：运动强度为主，亮度突变加成（特效类目标）。"""
    return round(shot.mean_motion_intensity + 0.5 * shot.luminance_spike_ratio, 4)


def match_targets(
    book: NarrativeTargetBook,
    shots: list[Shot],
    *,
    style_id: str | None = None,
) -> list[TargetMatch]:
    """逐目标求值；style_id 给定时只评估归属该风格的目标。

    对比类（pair）目标作用于相邻镜头：first 静 → second 动，
    间隔超过 pair_gap_seconds 的切点不视为连续铺垫。
    """
    matches: list[TargetMatch] = []
    for target in book.targets:
        if style_id and target.styles and style_id not in target.styles:
            continue
        if target.match == "pair":
            for first, second in zip(shots, shots[1:]):
                if second.start_seconds - first.end_seconds > target.pair_gap_seconds:
                    continue
                if not _eval_conditions(target.pair_first, first, "all"):
                    continue
                if not _eval_conditions(target.pair_second, second, "all"):
                    continue
                matches.append(
                    TargetMatch(
                        target_id=target.target_id,
                        kind="pair",
                        shot_indexes=(first.index, second.index),
                        start_seconds=first.start_seconds,
                        end_seconds=second.end_seconds,
                        score=round(second.mean_motion_intensity - first.mean_motion_intensity, 4),
                    )
                )
            continue
        for shot in shots:
            if _eval_conditions(target.conditions, shot, target.match):
                matches.append(
                    TargetMatch(
                        target_id=target.target_id,
                        kind="shot",
                        shot_indexes=(shot.index,),
                        start_seconds=shot.start_seconds,
                        end_seconds=shot.end_seconds,
                        score=_shot_score(target, shot),
                    )
                )
    matches.sort(key=lambda item: (-item.score, item.start_seconds))
    return matches


# ---------- 叙事模板重排（混剪） ----------


def _strategy_template(book: NarrativeTargetBook, template_id: str) -> NarrativeTemplate:
    for template in book.templates:
        if template.template_id == template_id:
            return template
    raise ValueError(f"unknown narrative template: {template_id}")


def _trim_budget(
    segments: list[dict[str, Any]], target_duration_seconds: float
) -> list[dict[str, Any]]:
    budget = target_duration_seconds
    clipped: list[dict[str, Any]] = []
    for segment in segments:
        if budget <= 0:
            break
        length = segment["end_seconds"] - segment["start_seconds"]
        if length > budget:
            segment = {**segment, "end_seconds": round(segment["start_seconds"] + budget, 3)}
            length = budget
        clipped.append(segment)
        budget -= length
    return clipped


def _to_segment(match: TargetMatch, role: str) -> dict[str, Any]:
    return {
        "start_seconds": match.start_seconds,
        "end_seconds": match.end_seconds,
        "shot_indexes": list(match.shot_indexes),
        "target_ids": [match.target_id],
        "score": match.score,
        "role": role,
    }


def plan_narrative(
    book: NarrativeTargetBook,
    matches: list[TargetMatch],
    template_id: str,
    *,
    target_duration_seconds: float = 15.0,
    interleave: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    """按叙事模板把命中重排为成片时间轴（返回 segments, notes）。

    - chronological：按原片时间顺序（事件->铺垫->转折->结果）；
    - peak_first：最高分命中置顶，其余按时间补因（事件->结果）；
    - contrast_open：最佳对比镜头对开场（平静->爆发，事件->转折）；
    - interleave：命中镜头按时间分两半交错（人物A行动->人物B行动），
      本地以「不同时间段≈不同人物」为近似，人物语义需 L2 标注确认。
    片段保持各自在原片内的完整性；重叠命中按时间合并去重。
    """
    template = _strategy_template(book, template_id)
    notes: list[str] = []
    if not matches:
        return [], ["no target matched; nothing to plan"]

    # 去重：同一镜头区间保留最高分命中，合并 target_ids
    merged: dict[tuple[float, float], TargetMatch] = {}
    target_ids: dict[tuple[float, float], list[str]] = {}
    for match in sorted(matches, key=lambda item: -item.score):
        key = (match.start_seconds, match.end_seconds)
        if key not in merged:
            merged[key] = match
            target_ids[key] = [match.target_id]
        elif match.target_id not in target_ids[key]:
            target_ids[key].append(match.target_id)

    ordered: list[TargetMatch]
    if template.strategy == "chronological":
        ordered = sorted(merged.values(), key=lambda item: item.start_seconds)
    elif template.strategy == "peak_first":
        best = max(merged.values(), key=lambda item: item.score)
        rest = sorted(
            (item for item in merged.values() if item is not best),
            key=lambda item: item.start_seconds,
        )
        ordered = [best, *rest]
    else:  # contrast_open
        pairs = [item for item in merged.values() if item.kind == "pair"]
        opener = max(pairs, key=lambda item: item.score) if pairs else None
        if opener is None:
            notes.append("contrast_open requested but no pair matched; fell back to peak_first")
            best = max(merged.values(), key=lambda item: item.score)
            rest = sorted(
                (item for item in merged.values() if item is not best),
                key=lambda item: item.start_seconds,
            )
            ordered = [best, *rest]
        else:
            rest = sorted(
                (
                    item
                    for item in merged.values()
                    if item is not opener and item.start_seconds >= opener.end_seconds
                ),
                key=lambda item: item.start_seconds,
            )
            ordered = [opener, *rest]

    if interleave:
        if not book.interleave.enabled:
            notes.append("interleave disabled in narrative config; ignored")
        else:
            chronological = sorted(ordered, key=lambda item: item.start_seconds)
            limit = book.interleave.max_pairs * 2
            chronological = chronological[: max(limit, 2)]
            half = len(chronological) // 2
            first_half, second_half = chronological[:half], chronological[half:]
            interleaved: list[TargetMatch] = []
            for left, right in zip(first_half, second_half):
                interleaved.extend([left, right])
            interleaved.extend(first_half[len(second_half):] + second_half[len(first_half):])
            ordered = interleaved
            notes.append(
                "interleave is a local approximation (temporal halves as character proxy); "
                "character/action labels require L2 narrative_board review"
            )

    segments: list[dict[str, Any]] = []
    used: set[int] = set()
    for position, match in enumerate(ordered):
        # 同一片段不重复使用（混剪也避免原片区间重叠造成画面重复）
        key_shots = set(match.shot_indexes)
        if key_shots & used:
            continue
        used |= key_shots
        role = "opening" if position == 0 and template.strategy != "chronological" else "body"
        segment = _to_segment(match, role)
        segment["target_ids"] = [
            target_id
            for key, ids in target_ids.items()
            if key == (match.start_seconds, match.end_seconds)
            for target_id in ids
        ]
        segments.append(segment)
    planned = _trim_budget(segments, target_duration_seconds)
    if len(planned) < len(segments):
        notes.append("timeline truncated to target duration")
    return planned, notes
