"""视觉分析服务：closed-set 图文相似度分类（SigLIP2），支持拒答。

沿用 Framework ClosedSetVisualEvidenceProvider 的方法：受限可审计目录 +
余弦相似度排序 + margin/score 双阈值拒答。区别：
1. 通过 HTTP 请求驱动，目录允许按请求扩展（受限规模）；
2. 内置目录面向新任务扩展了「风格信号」组（战斗/特效/特写等）；
3. 模型懒加载；未安装依赖时以确定性 Fake provider 支撑开发联调。
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Mapping, Sequence
from math import exp, sqrt
from pathlib import Path
from typing import Protocol

from .config import Settings
from .models import RankedVisualCandidate, VisualEvidenceItem

# 内置目录：前 5 组沿用 Framework 默认，后 2 组为新任务风格信号扩展。
# 目录必须受限可审计——这是分类，不是自由描述，因此支持显式拒答。
DEFAULT_VISUAL_CATALOG: dict[str, dict[str, str]] = {
    "time_of_day": {
        "night": "a video frame filmed at night",
        "dusk": "a video frame at dusk or in the evening",
        "daytime": "a video frame in daylight",
    },
    "action": {
        "fighting": "people are fighting",
        "riding_horse": "people are riding horses",
        "walking_away": "a person is walking away from the camera",
        "no_listed_action": "none of the listed actions is visible",
    },
    "object": {
        "car": "a car is clearly visible",
        "helmet": "a person wearing a helmet",
        "no_listed_object": "none of the listed objects is visible",
    },
    # ---- 新任务扩展：风格信号 ----
    "shot_size": {
        "close_up": "a close-up shot of a person's face",
        "medium_shot": "a medium shot of people from the waist up",
        "wide_shot": "a wide shot showing a large scene or landscape",
    },
    "visual_effect": {
        "explosion": "a large explosion or fire burst is visible",
        "magic_energy": "glowing magical energy or light beams",
        "speed_lines": "fast motion blur suggesting high speed",
        "no_effect": "no obvious special effect is visible",
    },
    "mood": {
        "sad": "a melancholic scene, dim light, a sad person",
        "tense": "a tense confrontational scene",
        "warm": "a warm cozy scene with soft light",
        "neutral": "none of the listed moods clearly applies",
    },
}

DEFAULT_MINIMUM_SCORES: dict[str, float] = {
    "time_of_day": 0.05,
    "action": 0.10,
    "object": 0.10,
    "shot_size": 0.08,
    "visual_effect": 0.10,
    "mood": 0.08,
}

MAX_CATALOG_GROUPS = 8
MAX_CATALOG_VALUES_PER_GROUP = 12
MIN_CATALOG_VALUES_PER_GROUP = 2


class EmbeddingProvider(Protocol):
    name: str

    def embed_images(self, image_paths: Sequence[Path]) -> list[list[float]]: ...

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]: ...


class FakeEmbeddingProvider:
    """确定性哈希 embedding：无模型环境下的开发联调模式。

    对同一 (内容) 产生稳定向量；文本与图像共享"内容相同则向量相同"的
    构造，使 closed-set 排序逻辑可被端到端测试。不用于生产判断。
    """

    name = "fake"

    def __init__(self, dim: int = 64):
        self.dim = dim

    def _vector(self, key: str) -> list[float]:
        digest = hashlib.sha256(key.encode("utf-8")).digest()
        raw = [digest[i % len(digest)] / 255.0 - 0.5 for i in range(self.dim)]
        return raw

    def embed_images(self, image_paths: Sequence[Path]) -> list[list[float]]:
        return [self._vector(f"image:{Path(p).name}") for p in image_paths]

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(f"text:{text}") for text in texts]


class Siglip2EmbeddingProvider:
    """真实 SigLIP2 图文 embedding，懒加载（首次调用时下载/载入模型）。"""

    name = "siglip2"

    def __init__(self, model_id: str = "google/siglip2-base-patch16-224"):
        self.model_id = model_id
        self._model = None
        self._processor = None
        self._lock = threading.Lock()

    def _ensure_loaded(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            try:
                import torch
                from transformers import AutoModel, AutoProcessor
            except ImportError as exc:
                raise RuntimeError(
                    "SigLIP2 requires `pip install transformers torch`"
                ) from exc
            self._processor = AutoProcessor.from_pretrained(self.model_id)
            self._model = AutoModel.from_pretrained(self.model_id)
            self._model.eval()
            self._torch = torch

    def _embed(self, inputs: dict) -> list[list[float]]:
        import numpy as np

        self._ensure_loaded()
        with self._torch.no_grad():
            outputs = self._model(**inputs)
        pool = outputs.pooler_output
        vectors = pool / pool.norm(dim=-1, keepdim=True)
        return vectors.cpu().numpy().astype("float32").tolist()

    def embed_images(self, image_paths: Sequence[Path]) -> list[list[float]]:
        from PIL import Image

        images = [Image.open(p).convert("RGB") for p in image_paths]
        inputs = self._processor(images=images, return_tensors="pt")
        return self._embed(inputs)

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        inputs = self._processor(text=list(texts), return_tensors="pt")
        return self._embed(inputs)


def _normalize(values: Sequence[float]) -> tuple[float, ...]:
    norm = sqrt(sum(value * value for value in values))
    if norm == 0:
        raise ValueError("embedding must not be zero")
    return tuple(value / norm for value in values)


def resolve_catalog(
    request_catalog: Mapping[str, Mapping[str, str]] | None,
) -> dict[str, dict[str, str]]:
    if request_catalog is None:
        return {group: dict(values) for group, values in DEFAULT_VISUAL_CATALOG.items()}
    merged = {group: dict(values) for group, values in DEFAULT_VISUAL_CATALOG.items()}
    for group, values in request_catalog.items():
        if not isinstance(group, str) or not group.strip():
            raise ValueError("catalog group names must be non-empty strings")
        if not isinstance(values, Mapping):
            raise ValueError(f"catalog group '{group}' must be a mapping")
        if len(values) < MIN_CATALOG_VALUES_PER_GROUP:
            raise ValueError(
                f"catalog group '{group}' requires at least "
                f"{MIN_CATALOG_VALUES_PER_GROUP} values"
            )
        if len(values) > MAX_CATALOG_VALUES_PER_GROUP:
            raise ValueError(
                f"catalog group '{group}' supports at most "
                f"{MAX_CATALOG_VALUES_PER_GROUP} values"
            )
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError(f"catalog group '{group}' values must be non-empty strings")
        if any(not isinstance(prompt, str) or not prompt.strip() for prompt in values.values()):
            raise ValueError(f"catalog group '{group}' prompts must be non-empty strings")
        merged[group] = dict(values)
    if len(merged) > MAX_CATALOG_GROUPS:
        raise ValueError(
            f"catalog supports at most {MAX_CATALOG_GROUPS} groups after merging defaults"
        )
    return merged


def answer_question(
    provider: EmbeddingProvider,
    *,
    question_type: str,
    catalog: Mapping[str, Mapping[str, str]],
    image_paths: Sequence[Path],
    timestamps_seconds: Sequence[float],
    minimum_margin: float = 0.01,
    minimum_score: float | None = None,
) -> VisualEvidenceItem:
    if question_type not in catalog:
        raise ValueError(f"unknown visual question type: {question_type}")
    if len(catalog[question_type]) < MIN_CATALOG_VALUES_PER_GROUP:
        raise ValueError("visual question catalog requires at least two candidates")
    if not 2 <= len(image_paths) <= 3 or len(image_paths) != len(timestamps_seconds):
        raise ValueError("visual evidence requires 2-3 aligned frames")

    image_rows = [_normalize(row) for row in provider.embed_images(image_paths)]
    entries = list(catalog[question_type].items())
    text_rows = [
        _normalize(row) for row in provider.embed_texts([prompt for _, prompt in entries])
    ]
    ranked = sorted(
        (
            RankedVisualCandidate(
                value=value,
                prompt=prompt,
                score=max(
                    sum(left * right for left, right in zip(image, text, strict=True))
                    for image in image_rows
                ),
            )
            for (value, prompt), text in zip(entries, text_rows, strict=True)
        ),
        key=lambda item: (-item.score, item.value),
    )
    margin = ranked[0].score - ranked[1].score
    threshold = (
        minimum_score
        if minimum_score is not None
        else DEFAULT_MINIMUM_SCORES.get(question_type, 0.10)
    )
    answered = ranked[0].score >= threshold and margin >= minimum_margin
    confidence = 1 / (1 + exp(-10 * margin))
    return VisualEvidenceItem(
        question_type=question_type,
        status="answered" if answered else "abstained",
        answer=ranked[0].value if answered else None,
        confidence=confidence,
        margin=margin,
        timestamps_seconds=list(timestamps_seconds),
        ranked_candidates=ranked,
        notes=[
            f"provider={provider.name}; closed-set similarity, not free-form VLM",
            f"acceptance thresholds: score>={threshold:.3f}, margin>={minimum_margin:.3f}",
            "human review required before treating a candidate as ground truth",
        ],
    )


def get_provider(settings: Settings) -> EmbeddingProvider:
    """按配置返回 embedding provider；fake 模式用于无模型环境。"""
    if settings.visual_fake_mode:
        return FakeEmbeddingProvider()
    return Siglip2EmbeddingProvider()
