from __future__ import annotations

from typing import Any

from app.llm.api_client import sanitize_tool_conversation_messages
from app.llm.prompts.runtime import estimate_prompt_tokens


MAX_MODEL_CONTEXT_MESSAGES = 60
# 原先按裸字符数(40_000)砍历史：中日文场景下 1 字≈1 token，尚可接受；但英文/代码
# 粘贴场景 1 token≈4 字符，同样的字符预算会白白少留很多轮真实对话。改用已有的
# estimate_prompt_tokens 按 token 记账，数值维持同一量级，中日文场景行为基本不变，
# ASCII/代码为主的场景能少裁一些不必要的历史。
MAX_MODEL_CONTEXT_TOKENS = 40_000
# 向后兼容旧常量名，避免外部/测试引用报错。
MAX_MODEL_CONTEXT_CHARS = MAX_MODEL_CONTEXT_TOKENS
# 多模态截图在入模裁剪时按固定 token 粗估，避免低估后把 assistant+tool 链裁断。
ESTIMATED_IMAGE_PART_TOKENS = 1200
# 始终保持最近 N 个用户发言的上下文不裁剪——确保「刚才说了什么」不会因
# 消息数或 token 预算耗尽而丢失。DeepSeek V4 前缀缓存场景下多保留几轮
# 对话对延迟影响极小（TTFT 变化在 100ms 以内）。
MIN_KEEP_USER_TURNS = 8


def trim_messages_for_model(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """保留最近上下文：优先保证最近 N 轮对话完整，再用 token 预算兜底。

    策略（从后往前构建）：
    1. 始终保留最近 MIN_KEEP_USER_TURNS 个用户发言对应的完整上下文
    2. 剩余位置按 token 预算填充更早的消息
    3. 超出预算时优先丢弃旧 tool 消息，保留对话。
    """
    if not messages:
        return []

    # Step 1: find the position where we hit MIN_KEEP_USER_TURNS user messages
    user_count = 0
    keep_from = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        role = str(messages[i].get("role", "")).strip()
        keep_from = i
        if role == "user":
            user_count += 1
            if user_count >= MIN_KEEP_USER_TURNS:
                break

    # Step 2: can we fit more? Walk backwards adding messages until token budget hit.
    # Prefer dialogue over tool messages when budget is tight.
    if keep_from > 0:
        remaining = []
        remaining_tokens = 0
        for i in range(keep_from - 1, -1, -1):
            msg = messages[i]
            t = _estimate_message_tokens(msg)
            current_total = _estimate_messages_tokens(
                remaining + messages[keep_from:]
            )
            if current_total + t > MAX_MODEL_CONTEXT_TOKENS:
                break
            remaining.insert(0, msg)
        keep_from -= len(remaining)

    recent = list(messages[keep_from:])

    # Step 3: if still over budget (should be rare — only when guaranteed turns
    # themselves are enormous), trim tool messages first, then oldest dialogue
    # that is NOT part of the guaranteed set.
    guaranteed_count = len(messages) - keep_from
    while len(recent) > 1 and _estimate_messages_tokens(recent) > MAX_MODEL_CONTEXT_TOKENS:
        # Try to find a non-essential tool message to drop first
        dropped = False
        for i, msg in enumerate(recent):
            if i >= guaranteed_count:
                break  # don't touch guaranteed messages
            role = str(msg.get("role", "")).strip()
            if role == "tool":
                # Don't drop tool immediately after its assistant call
                if i == 0 or str(recent[i - 1].get("role", "")).strip() != "assistant":
                    recent.pop(i)
                    guaranteed_count -= 1
                    dropped = True
                    break
        if not dropped:
            # Drop oldest non-guaranteed dialogue
            if guaranteed_count > 0 and len(recent) > guaranteed_count:
                recent.pop(0)
                guaranteed_count -= 1
            else:
                break  # can't trim further without breaking guarantee

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
