from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any



DEBUG_KEY = "SAKURA_DEBUG"
DEBUG_BODY_KEY = "SAKURA_DEBUG_BODY"
_TRUE_VALUES = {"1", "true", "yes", "on"}
_SENSITIVE_KEY_MARKERS = ("api_key", "authorization", "token", "secret", "password")
_BODY_KEY_MARKERS = (
    "body",
    "content",
    "messages",
    "prompt",
    "reply",
    "response",
    "system_prompt",
    "text",
)
_MAX_TEXT_CHARS = 600
_MAX_BODY_CHARS = 8000
_MAX_BODY_SUMMARY_CHARS = 160
_MAX_LIST_ITEMS = 8
_MAX_DICT_ITEMS = 24


def debug_enabled() -> bool:
    """判断是否开启终端调试日志。"""
    return _read_bool(DEBUG_KEY, default=False)


def debug_body_enabled() -> bool:
    """判断调试日志是否允许输出完整正文。"""
    return debug_enabled() and _read_bool(DEBUG_BODY_KEY, default=False)


def debug_log(category: str, message: str, data: Any | None = None) -> None:
    """按统一格式向终端输出调试日志，默认关闭。"""
    if not debug_enabled():
        return

    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    line = f"[Debug][{category}][{timestamp}] {message}"
    if data is not None:
        line = f"{line} {format_debug_data(data)}"
    print(line)


def format_debug_data(data: Any) -> str:
    """格式化调试数据，供测试和日志输出复用。"""
    safe_data = sanitize_debug_data(data, include_body=debug_body_enabled())
    return json.dumps(safe_data, ensure_ascii=False, default=str)


def sanitize_debug_data(data: Any, include_body: bool | None = None) -> Any:
    """脱敏并截断调试数据。include_body=False 时只保留正文摘要。"""
    if include_body is None:
        include_body = debug_body_enabled()
    return _sanitize_value(data, include_body=include_body, body_context=False)


def summarize_text(text: str, max_chars: int = _MAX_BODY_SUMMARY_CHARS) -> dict[str, Any]:
    """生成正文摘要，避免默认日志泄露完整内容。"""
    return {
        "type": "text",
        "chars": len(text),
        "preview": _truncate_text(text, max_chars),
    }


def summarize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """摘要化 OpenAI 兼容消息列表。"""
    summarized: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        content = message.get("content")
        item: dict[str, Any] = {
            "index": index,
            "role": message.get("role", ""),
        }
        if isinstance(content, str):
            item["content"] = summarize_text(content)
        elif isinstance(content, list):
            item["content"] = [_summarize_content_part(part) for part in content[:_MAX_LIST_ITEMS]]
            if len(content) > _MAX_LIST_ITEMS:
                item["omitted_parts"] = len(content) - _MAX_LIST_ITEMS
        else:
            item["content_type"] = type(content).__name__
        summarized.append(item)
    return summarized


def _summarize_content_part(part: Any) -> Any:
    if not isinstance(part, dict):
        return {"type": type(part).__name__}
    part_type = part.get("type")
    if part_type == "text":
        return {"type": "text", "text": summarize_text(str(part.get("text", "")))}
    if part_type == "image_url":
        return {"type": "image_url", "image_url": "<image omitted>"}
    return {"type": part_type or "unknown", "keys": sorted(str(key) for key in part.keys())}


def _sanitize_value(value: Any, *, include_body: bool, body_context: bool) -> Any:
    if isinstance(value, dict):
        return _sanitize_dict(value, include_body=include_body, body_context=body_context)
    if isinstance(value, list):
        items = [
            _sanitize_value(item, include_body=include_body, body_context=body_context)
            for item in value[:_MAX_LIST_ITEMS]
        ]
        if len(value) > _MAX_LIST_ITEMS:
            items.append({"omitted_items": len(value) - _MAX_LIST_ITEMS})
        return items
    if isinstance(value, tuple):
        return _sanitize_value(list(value), include_body=include_body, body_context=body_context)
    if isinstance(value, bytes):
        return {"type": "bytes", "bytes": len(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        if _looks_like_image_data_url(value):
            return {"type": "image_data_url", "chars": len(value)}
        if body_context and not include_body:
            return summarize_text(value)
        if body_context and include_body:
            return _truncate_text(value, _MAX_BODY_CHARS)
        return _truncate_text(value, _MAX_TEXT_CHARS)
    return value


def _sanitize_dict(value: dict[Any, Any], *, include_body: bool, body_context: bool) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    items = list(value.items())
    for key, item_value in items[:_MAX_DICT_ITEMS]:
        key_text = str(key)
        normalized_key = key_text.lower()
        if _is_sensitive_key(normalized_key):
            sanitized[key_text] = "<redacted>"
            continue
        if normalized_key == "messages" and isinstance(item_value, list):
            sanitized[key_text] = (
                _sanitize_value(item_value, include_body=include_body, body_context=False)
                if include_body
                else summarize_messages([item for item in item_value if isinstance(item, dict)])
            )
            continue
        if normalized_key == "content" and isinstance(item_value, list) and not include_body:
            summarized_parts = [_summarize_content_part(part) for part in item_value[:_MAX_LIST_ITEMS]]
            if len(item_value) > _MAX_LIST_ITEMS:
                summarized_parts.append({"omitted_items": len(item_value) - _MAX_LIST_ITEMS})
            sanitized[key_text] = summarized_parts
            continue
        next_body_context = body_context or _is_body_key(normalized_key)
        sanitized[key_text] = _sanitize_value(
            item_value,
            include_body=include_body,
            body_context=next_body_context,
        )
    if len(items) > _MAX_DICT_ITEMS:
        sanitized["omitted_keys"] = len(items) - _MAX_DICT_ITEMS
    return sanitized


def _is_sensitive_key(normalized_key: str) -> bool:
    return any(marker in normalized_key for marker in _SENSITIVE_KEY_MARKERS)


def _is_body_key(normalized_key: str) -> bool:
    return any(marker in normalized_key for marker in _BODY_KEY_MARKERS)


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}...<truncated {len(text) - max_chars} chars>"


def _looks_like_image_data_url(text: str) -> bool:
    return text.startswith("data:image/")


def _read_bool(key: str, default: bool) -> bool:
    debug_values = _load_debug_values()
    alias = "enabled" if key == DEBUG_KEY else "body_enabled"
    value = debug_values.get(alias, debug_values.get(key))
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE_VALUES


def _load_debug_values() -> dict[str, Any]:
    from app.config.yaml_config import load_yaml_mapping
    config_path = Path(__file__).resolve().parents[1] / "data" / "config" / "system_config.yaml"
    try:
        system_config = load_yaml_mapping(config_path)
    except (OSError, ValueError):
        return {}
    debug_config = system_config.get("debug")
    return dict(debug_config) if isinstance(debug_config, dict) else {}
