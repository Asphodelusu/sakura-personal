"""后台结构化 LLM 调用：统一 JSON 解析、路由与失败重试。

记忆反思、记忆整理、视觉摘要等任务都需要「低温 + json_object + 噪声容错」，
集中在此避免各模块各自修补。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.core.cancellation import CancelChecker, check_cancelled

_logger = logging.getLogger("JsonCompletion")


class BackgroundJsonError(RuntimeError):
    """后台 JSON 任务最终失败（含重试）。"""

DEFAULT_BACKGROUND_TEMPERATURE = 0.2
DEFAULT_REPAIR_TEMPERATURE = 0.1
DEFAULT_JSON_MAX_TOKENS = 2000

_JSON_RESPONSE_FORMAT = {"type": "json_object"}

_DEFAULT_REPAIR_USER_MESSAGE = (
    "上一条输出不是合法 JSON。请只返回严格 JSON，不要解释、不要推理、不要 Markdown。"
)


def load_json_object(raw: str) -> dict[str, Any]:
    """从模型输出解析 JSON 对象；容忍代码块与前后缀噪声。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}


def complete_background_json(
    api_client: Any,
    system_prompt: str,
    messages: list[dict[str, str]],
    *,
    temperature: float = DEFAULT_BACKGROUND_TEMPERATURE,
    max_tokens: int = DEFAULT_JSON_MAX_TOKENS,
    cancel_checker: CancelChecker | None = None,
    retry_on_invalid: bool = True,
    repair_user_message: str = _DEFAULT_REPAIR_USER_MESSAGE,
    log_label: str = "BackgroundJson",
) -> tuple[dict[str, Any], str]:
    """调用后台 JSON 任务；解析失败时可自动追加一次修复请求。

    返回 (parsed_dict, last_raw_text)。parsed 为空 dict 表示未能得到有效 JSON。
    """
    check_cancelled(cancel_checker)
    raw = _invoke_complete_raw(
        api_client,
        system_prompt,
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        cancel_checker=cancel_checker,
    )
    parsed = load_json_object(raw)
    if parsed or not retry_on_invalid or not raw.strip():
        if not parsed and raw.strip():
            _logger.warning("%s: invalid JSON from LLM, raw=%s", log_label, raw.strip()[:200])
        return parsed, raw

    repair_messages = [
        *messages,
        {"role": "assistant", "content": raw},
        {"role": "user", "content": repair_user_message},
    ]
    try:
        repaired_raw = _invoke_complete_raw(
            api_client,
            system_prompt,
            repair_messages,
            temperature=DEFAULT_REPAIR_TEMPERATURE,
            max_tokens=max_tokens,
            cancel_checker=cancel_checker,
        )
    except Exception as exc:
        _logger.exception("%s: JSON repair call failed", log_label)
        raise BackgroundJsonError(f"{log_label}: JSON repair call failed") from exc

    parsed = load_json_object(repaired_raw)
    if not parsed:
        _logger.warning("%s: invalid JSON after repair, raw=%s", log_label, repaired_raw.strip()[:200])
        raise BackgroundJsonError(f"{log_label}: invalid JSON after retry")
    return parsed, repaired_raw


def _invoke_complete_raw(
    api_client: Any,
    system_prompt: str,
    messages: list[dict[str, str]],
    *,
    temperature: float,
    max_tokens: int,
    cancel_checker: CancelChecker | None,
) -> str:
    kwargs: dict[str, Any] = {
        "temperature": temperature,
        "response_format": _JSON_RESPONSE_FORMAT,
        "max_tokens": max_tokens,
        "cancel_checker": cancel_checker,
        "task": "background",
        "thinking": {"type": "disabled"},
    }
    return api_client.complete_raw(system_prompt, messages, **kwargs)
