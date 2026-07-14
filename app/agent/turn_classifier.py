"""轻量 LLM 分类器：判断用户输入为 simple 或 deep。"""

from __future__ import annotations

import json
import re
from dataclasses import is_dataclass, replace
from typing import Any, Literal

from app.core.debug_log import debug_log
from app.llm.api_client import ApiRequestError, OpenAICompatibleClient

TurnDepth = Literal["simple", "deep"]

_CLASSIFIER_SYSTEM_PROMPT = (
    "你是 Sakura 的对话路由分类器。根据用户最新一句话，判断本轮应答复杂度。\n"
    "只返回 JSON：{\"depth\": \"simple\"} 或 {\"depth\": \"deep\"}。\n"
    "simple：寒暄、确认、简短闲聊，无需工具或深度推理。\n"
    "deep：需要查资料、操作电脑、记忆、规划、长文分析或复杂任务。"
)

_JSON_DEPTH_PATTERN = re.compile(r'"depth"\s*:\s*"(simple|deep)"', re.IGNORECASE)


def classify_turn_depth(
    user_text: str,
    *,
    client: OpenAICompatibleClient,
    timeout_seconds: int = 3,
) -> TurnDepth | None:
    text = user_text.strip()
    if not text:
        return None
    short_client = _client_with_timeout(client, timeout_seconds)
    try:
        raw = short_client.complete_raw(
            _CLASSIFIER_SYSTEM_PROMPT,
            [{"role": "user", "content": text}],
            temperature=0.0,
            max_tokens=32,
            thinking={"type": "disabled"},
        )
    except (ApiRequestError, TimeoutError, OSError) as exc:
        debug_log(
            "TurnClassifier",
            "分类器调用失败，回退 standard",
            {"error": str(exc)},
        )
        return None
    return _parse_depth(raw)


def _client_with_timeout(
    client: OpenAICompatibleClient,
    timeout_seconds: int,
) -> OpenAICompatibleClient:
    settings = getattr(client, "settings", None)
    if settings is None or not is_dataclass(settings):
        return client
    current_timeout = getattr(settings, "timeout_seconds", timeout_seconds)
    if current_timeout <= timeout_seconds:
        return client
    return OpenAICompatibleClient(replace(settings, timeout_seconds=timeout_seconds))


def _parse_depth(raw: str) -> TurnDepth | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        payload: dict[str, Any] = json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_DEPTH_PATTERN.search(text)
        if match is None:
            return None
        depth = match.group(1).lower()
        return depth if depth in {"simple", "deep"} else None
    depth = str(payload.get("depth", "")).strip().lower()
    if depth in {"simple", "deep"}:
        return depth  # type: ignore[return-value]
    return None
