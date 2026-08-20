"""L2 云端复核：OpenAI-compatible 多模态 Chat Completions 适配器。"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Settings
from .models import CandidateWindow, L2ReviewItem, SignalValues
from .style_profiles import RuleBook, StyleProfile


class L2ServiceError(RuntimeError):
    """云端 L2 调用或响应契约失败。"""


def review_profiles(
    settings: Settings,
    rule_book: RuleBook,
    *,
    signals: SignalValues,
    candidates: list[CandidateWindow],
    profile_ids: list[str],
    image_urls: list[str],
) -> list[L2ReviewItem]:
    if not settings.l2_enabled:
        raise L2ServiceError("L2 review disabled; set SMARTCLIP_L2_ENABLED=1")
    if not settings.l2_api_key or not settings.l2_base_url:
        raise L2ServiceError("L2 requires SMARTCLIP_L2_API_KEY and SMARTCLIP_L2_BASE_URL")

    profiles = {profile.profile_id: profile for profile in rule_book.profiles}
    items: list[L2ReviewItem] = []
    for profile_id in profile_ids:
        profile = profiles.get(profile_id)
        if profile is None or profile.l2_fallback is None:
            raise L2ServiceError(f"profile '{profile_id}' has no L2 fallback configuration")
        selected = sorted(candidates, key=lambda candidate: (-candidate.score, candidate.start_seconds))[
            : profile.l2_fallback.max_windows
        ]
        prompt = _load_prompt(rule_book, profile)
        content = _request_content(prompt, signals, selected, image_urls)
        raw = _chat_completion(settings, content)
        items.append(
            L2ReviewItem(
                profile_id=profile_id,
                prompt_ref=profile.l2_fallback.prompt_ref,
                model=settings.l2_model,
                result=_parse_json_or_wrap(raw),
                raw_content=raw,
            )
        )
    return items


def _load_prompt(rule_book: RuleBook, profile: StyleProfile) -> str:
    assert profile.l2_fallback is not None
    if not rule_book.source_path:
        raise L2ServiceError("rule book source path unavailable; cannot resolve prompt")
    project_root = Path(rule_book.source_path).resolve().parent.parent
    source = project_root / profile.l2_fallback.prompt_ref
    if not source.is_file():
        raise L2ServiceError(f"L2 prompt not found: {source}")
    return source.read_text(encoding="utf-8")


def _request_content(
    prompt: str,
    signals: SignalValues,
    candidates: list[CandidateWindow],
    image_urls: list[str],
) -> list[dict[str, object]]:
    evidence = {
        "signals": signals.model_dump(),
        "candidates": [candidate.model_dump() for candidate in candidates],
        "instruction": "Return only one JSON object matching the prompt output schema.",
    }
    content: list[dict[str, object]] = [
        {"type": "text", "text": f"{prompt}\n\nINPUT:\n{json.dumps(evidence, ensure_ascii=False)}"}
    ]
    content.extend({"type": "image_url", "image_url": {"url": url}} for url in image_urls)
    return content


def _chat_completion(settings: Settings, content: list[dict[str, object]]) -> str:
    payload = {
        "model": settings.l2_model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "You are a precise video-review service. Follow the supplied rubric exactly.",
            },
            {"role": "user", "content": content},
        ],
    }
    request = Request(
        settings.l2_base_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.l2_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=settings.l2_timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[-500:]
        raise L2ServiceError(f"L2 provider returned HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise L2ServiceError(f"L2 provider request failed: {exc}") from exc
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise L2ServiceError("L2 provider response lacks choices[0].message.content") from exc
    if not isinstance(content, str) or not content.strip():
        raise L2ServiceError("L2 provider returned empty content")
    return content.strip()


def _parse_json_or_wrap(content: str) -> dict[str, object]:
    stripped = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        return {"verdict": "uncertain", "parse_error": True}
    return value if isinstance(value, dict) else {"verdict": "uncertain", "parse_error": True}
