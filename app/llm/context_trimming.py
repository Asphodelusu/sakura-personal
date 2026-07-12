from __future__ import annotations

from typing import Any

from app.llm.prompts.runtime import estimate_prompt_tokens


MAX_MODEL_CONTEXT_MESSAGES = 24
# 原先按裸字符数(40_000)砍历史：中日文场景下 1 字≈1 token，尚可接受；但英文/代码
# 粘贴场景 1 token≈4 字符，同样的字符预算会白白少留很多轮真实对话。改用已有的
# estimate_prompt_tokens 按 token 记账，数值维持同一量级，中日文场景行为基本不变，
# ASCII/代码为主的场景能少裁一些不必要的历史。
MAX_MODEL_CONTEXT_TOKENS = 40_000
# 向后兼容旧常量名，避免外部/测试引用报错。
MAX_MODEL_CONTEXT_CHARS = MAX_MODEL_CONTEXT_TOKENS


def trim_messages_for_model(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """保留最近上下文，并用 token 预算兜底限制入模历史体积。"""
    recent = list(messages[-MAX_MODEL_CONTEXT_MESSAGES:])
    while len(recent) > 1 and _estimate_messages_tokens(recent) > MAX_MODEL_CONTEXT_TOKENS:
        recent.pop(0)
    return recent


def _estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_prompt_tokens(str(message.get("content", ""))) for message in messages)
