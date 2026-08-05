"""主动事件消息构建 — 从 runtime.py 拆分的叶子模块。

纯函数、无实例状态；负责把主动事件（提醒/屏幕感知/互动）转成模型可读的
用户消息，并对最近对话与屏幕上下文做脱敏/截断。
"""

from __future__ import annotations

import json
from typing import Any

from app.agent.actions import AgentEvent
from app.agent.runtime_limits import (
    MAX_EVENT_RECENT_CONVERSATION_CONTENT_CHARS,
    MAX_EVENT_RECENT_CONVERSATION_MESSAGES,
)
from app.agent.screen_awareness import SCREEN_AWARENESS_IMAGE_DETAIL
from app.llm.api_client import ChatMessage


def _build_event_messages(event: AgentEvent) -> list[ChatMessage]:
    text = _format_event_for_model(event)
    image_parts = _build_event_screen_context_image_parts(event.payload)
    if not image_parts:
        return [{"role": "user", "content": text}]

    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": text,
                },
                *image_parts,
            ],
        }
    ]


def _build_event_screen_context_image_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    screen_contexts = payload.get("screen_contexts")
    image_parts: list[dict[str, Any]] = []
    if isinstance(screen_contexts, list):
        for screen_context in screen_contexts:
            if isinstance(screen_context, dict):
                image_part = _build_screen_context_image_part(screen_context)
                if image_part is not None:
                    image_parts.append(image_part)
    if image_parts:
        return image_parts

    screen_context = payload.get("screen_context")
    if isinstance(screen_context, dict):
        image_part = _build_screen_context_image_part(screen_context)
        if image_part is not None:
            return [image_part]
    return []


def _build_screen_context_image_part(screen_context: dict[str, Any]) -> dict[str, Any] | None:
    data_url = screen_context.get("data_url")
    if not isinstance(data_url, str) or not data_url.startswith("data:image/"):
        return None
    detail = _normalize_image_detail(
        screen_context.get("detail"),
        default=SCREEN_AWARENESS_IMAGE_DETAIL,
    )
    return {
        "type": "image_url",
        "image_url": {
            "url": data_url,
            "detail": detail,
        },
    }


def _normalize_image_detail(value: Any, *, default: str = "low") -> str:
    detail = str(value or "").strip().lower()
    if detail in {"low", "high", "original", "auto"}:
        return detail
    return default


def _format_event_for_model(event: AgentEvent) -> str:
    if event.type in {"screen_awareness_check", "proactive_check"}:
        instruction = "主动屏幕感知事件如下，请基于屏幕内容找话题：可以评论变化、接续任务、询问卡点，或保持安静；不要把时间或停留时长自动泛化成休息建议。"
    elif event.type == "user_interaction":
        action_text = event.payload.get("text", "对你做了一个动作")
        return f"（{action_text}）[请用角色语气直接回应这个互动，一句话，不超过20字。]"
    else:
        instruction = "主动事件如下，请生成要直接说给对方听的提醒："
    return instruction + "\n" + json.dumps(
        _redact_event_for_model(event),
        ensure_ascii=False,
        indent=2,
    )


def _redact_event_for_model(event: AgentEvent) -> dict[str, Any]:
    payload = dict(event.payload)
    recent_conversation = payload.get("recent_conversation")
    if isinstance(recent_conversation, list):
        payload["recent_conversation"] = _sanitize_event_recent_conversation(
            recent_conversation,
        )
    screen_context = payload.get("screen_context")
    if isinstance(screen_context, dict):
        payload["screen_context"] = _redact_screen_context_for_model(screen_context)
    screen_contexts = payload.get("screen_contexts")
    if isinstance(screen_contexts, list):
        payload["screen_contexts"] = [
            _redact_screen_context_for_model(screen_context)
            if isinstance(screen_context, dict)
            else screen_context
            for screen_context in screen_contexts
        ]
    return {
        "type": event.type,
        "payload": payload,
    }


def _sanitize_event_recent_conversation(
    recent_conversation: list[Any],
) -> list[dict[str, str]]:
    sanitized: list[dict[str, str]] = []
    for item in recent_conversation:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip()
        if role not in {"user", "assistant"}:
            continue
        content = item.get("content")
        if not isinstance(content, str):
            continue
        normalized_content = " ".join(content.split())
        if not normalized_content:
            continue
        sanitized.append(
            {
                "role": role,
                "content": _truncate_event_recent_conversation_content(
                    normalized_content,
                ),
            }
        )
    return sanitized[-MAX_EVENT_RECENT_CONVERSATION_MESSAGES:]


def _truncate_event_recent_conversation_content(content: str) -> str:
    if len(content) <= MAX_EVENT_RECENT_CONVERSATION_CONTENT_CHARS:
        return content
    return content[: MAX_EVENT_RECENT_CONVERSATION_CONTENT_CHARS - 1].rstrip() + "…"


def _redact_screen_context_for_model(screen_context: dict[str, Any]) -> dict[str, Any]:
    redacted_context = dict(screen_context)
    if redacted_context.pop("data_url", None):
        redacted_context["image_attached"] = True
    return redacted_context
