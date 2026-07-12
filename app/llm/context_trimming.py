from __future__ import annotations

from typing import Any

from app.llm.api_client import sanitize_tool_conversation_messages
from app.llm.prompts.runtime import estimate_prompt_tokens


MAX_MODEL_CONTEXT_MESSAGES = 24
# 原先按裸字符数(40_000)砍历史：中日文场景下 1 字≈1 token，尚可接受；但英文/代码
# 粘贴场景 1 token≈4 字符，同样的字符预算会白白少留很多轮真实对话。改用已有的
# estimate_prompt_tokens 按 token 记账，数值维持同一量级，中日文场景行为基本不变，
# ASCII/代码为主的场景能少裁一些不必要的历史。
MAX_MODEL_CONTEXT_TOKENS = 40_000
# 向后兼容旧常量名，避免外部/测试引用报错。
MAX_MODEL_CONTEXT_CHARS = MAX_MODEL_CONTEXT_TOKENS
# 多模态截图在入模裁剪时按固定 token 粗估，避免低估后把 assistant+tool 链裁断。
ESTIMATED_IMAGE_PART_TOKENS = 1200


def trim_messages_for_model(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """保留最近上下文，并用 token 预算兜底限制入模历史体积。"""
    recent = list(messages[-MAX_MODEL_CONTEXT_MESSAGES:])
    while len(recent) > 1 and _estimate_messages_tokens(recent) > MAX_MODEL_CONTEXT_TOKENS:
        recent.pop(0)
    return _normalize_trimmed_message_window(recent)


def _normalize_trimmed_message_window(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sanitized = sanitize_tool_conversation_messages(messages)
    while sanitized and str(sanitized[0].get("role", "")).strip() == "tool":
        sanitized.pop(0)
    return sanitized


def _estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(_estimate_message_tokens(message) for message in messages)


def _estimate_message_tokens(message: dict[str, Any]) -> int:
    content = message.get("content")
    if isinstance(content, list):
        tokens = 0
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image_url":
                tokens += ESTIMATED_IMAGE_PART_TOKENS
                continue
            if part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    tokens += estimate_prompt_tokens(text)
        return tokens
    return estimate_prompt_tokens(str(content or ""))
