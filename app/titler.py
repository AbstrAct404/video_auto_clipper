"""成品自动命名：把风格/目标命中/人物-行动线索映射为可直接发布的剪辑标题。

标题模板库依据公开爆款标题规律整理（黄金 3 秒钩子、悬念问句、
「【标签】描述」分区前缀、对话引用开场等，参考 B 站高燃混剪与抖音
短视频标题惯例），按风格分池：本地确定性生成、可复现（同输入同输出）；
人物-行动等语义线索由 L2（narrative_board）提供时注入 {subject}/{action}
槽位，生成个性化标题。文件名再做平台安全净化（去路径分隔符/保留字）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TitleContext:
    """标题生成上下文：风格线索 + 目标命中 + L2 人物-行动 + 信号特征。"""

    style_hint: str | None = None
    target_ids: tuple[str, ...] = ()
    character_actions: tuple[tuple[str, str], ...] = ()
    duration_seconds: float = 0.0
    peak_motion: float = 0.0
    audio_energy: float = 0.0


# 风格 → 标题模板池（槽位：{subject}/{action}/{seconds}）
_POOLS: dict[str, tuple[str, ...]] = {
    "ran_xiang": (
        "全程高燃！{seconds}秒肾上腺素拉满",
        "当雷霆落下，谁敢接这一击",
        "这爆发力直接封神！",
        "【高燃踩点】力量感拉满的名场面",
        "看好了！这一击会很帅",
    ),
    "daily_turbulence": (
        "平静不过三秒，反转说来就来",
        "上一秒岁月静好，下一秒直接起飞",
        "这剧情反转我给满分",
    ),
    "battle_effects": (
        "特效炸裂！每一帧都在燃烧经费",
        "雷电糊脸的一击，隔着屏幕都觉得疼",
        "【特效混剪】这就是经费燃烧的声音",
    ),
    "romance_intimacy": (
        "甜度超标警告！这一幕心动了",
        "这个眼神我先磕为敬",
    ),
    "fast_dialogue": (
        "台词快到截图都来不及！",
        "全程无尿点，对白密度拉满",
    ),
    "group_action": (
        "{subject}出手的瞬间，全场安静了",
        "一人一个名场面，群像的压迫感",
        "{subject}：{action}，就这么简单",
    ),
    "generic": (
        "看到最后才懂这个镜头的含金量",
        "这条片子后劲太大了",
        "第3秒开始就不想划走了",
    ),
}

# 目标 id → 模板池路由（一个目标可命中多池）
_TARGET_POOL: dict[str, tuple[str, ...]] = {
    "battle_effects": ("battle_effects", "ran_xiang"),
    "blood_intensity": ("battle_effects", "ran_xiang"),
    "daily_turbulence": ("daily_turbulence",),
    "romance_intimacy": ("romance_intimacy",),
    "group_action": ("group_action",),
}


def _fill(template: str, context: TitleContext) -> str:
    """槽位填充：无对应线索的模板直接判废（返回空串）。"""
    if "{seconds}" in template:
        seconds = max(1, int(round(context.duration_seconds)))
        template = template.replace("{seconds}", str(seconds))
    if "{subject}" in template or "{action}" in template:
        pairs = context.character_actions
        if not pairs:
            return ""
        subject, action = pairs[0]
        template = template.replace("{subject}", subject).replace("{action}", action)
    return template


def _length_score(title: str) -> float:
    """标题长度分：6~22 字为甜点区（黄金 3 秒标题惯例），超长短罚。"""
    length = len(title)
    if length <= 0:
        return -10.0
    if 6 <= length <= 22:
        return 2.0
    return -abs(length - 14) * 0.15


def _score(title: str, context: TitleContext, index: int) -> float:
    score = _length_score(title)
    # 悬念问句 / 惊叹钩子加成（爆款标题常见钩子形态）
    if "？" in title or "?" in title:
        score += 1.0
    if "！" in title or "!" in title:
        score += 0.5
    # 有人物-行动线索的个性化标题优先（群像「人物→行动」叙事）
    if context.character_actions and any(
        pair[0] in title for pair in context.character_actions
    ):
        score += 1.5
    # 确定性 tiebreak：同一输入永远选同一个标题
    digest = hashlib.sha256(f"{title}#{index}".encode("utf-8")).digest()
    return score + digest[0] / 1000.0


def generate_titles(context: TitleContext, *, max_candidates: int = 3) -> list[str]:
    """生成候选标题：首条为推荐标题（最高分），其余为备选。

    模板池路由：风格 hint → 命中目标对应池 → 兜底池，逐池去重展开。
    """
    pool_keys: list[str] = []
    if context.style_hint and context.style_hint in _POOLS:
        pool_keys.append(context.style_hint)
    for target_id in context.target_ids:
        for key in _TARGET_POOL.get(target_id, ()):
            if key not in pool_keys:
                pool_keys.append(key)
    # 有人物-行动线索时纳入群像池（「人物→行动」个性化标题）
    if context.character_actions and "group_action" not in pool_keys:
        pool_keys.append("group_action")
    if "generic" not in pool_keys:
        pool_keys.append("generic")

    candidates: list[str] = []
    seen: set[str] = set()
    for key in pool_keys:
        for template in _POOLS[key]:
            filled = _fill(template, context)
            if filled and filled not in seen:
                seen.add(filled)
                candidates.append(filled)
    if not candidates:
        candidates = list(_POOLS["generic"])
    ranked = sorted(
        enumerate(candidates),
        key=lambda pair: (-_score(pair[1], context, pair[0]), pair[0]),
    )
    return [title for _, title in ranked[:max_candidates]]


_INVALID = '<>:"/\\|*'
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", "com1", "com2", "com3", "com4"}


def sanitize_title(title: str, *, max_length: int = 60) -> str:
    """标题 → 文件名安全形式：去路径分隔符与 Windows 非法字符，保留中文。"""
    cleaned = "".join(
        char for char in title.strip() if char not in _INVALID and ord(char) >= 32
    )
    cleaned = cleaned.strip().strip(".")[:max_length].strip()
    if not cleaned or cleaned.lower() in _WINDOWS_RESERVED:
        return "untitled_clip"
    return cleaned


def product_filename(title: str, existing: set[str]) -> str:
    """标题 → 唯一成片文件名；重名追加短序号，杜绝覆盖旧成品。"""
    base = sanitize_title(title)
    name = f"{base}.mp4"
    suffix = 2
    while name in existing:
        name = f"{base}_{suffix}.mp4"
        suffix += 1
    return name


def title_for_path(output_path: str | Path) -> str:
    """成片路径 → 展示标题（去扩展名，供回显/发布文案）。"""
    return Path(output_path).stem
