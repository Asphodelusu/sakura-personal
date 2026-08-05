"""工具消息构建与脱敏 — 从 runtime.py 拆分的叶子模块。

纯函数、无实例状态；负责把 ToolExecutionResult 变成模型可读的 tool 消息，
以及对含图/超长内容做脱敏与截断。
"""

from __future__ import annotations

import json
from typing import Any

from app.agent.actions import PendingToolAction
from app.agent.runtime_limits import (
    MAX_PENDING_CONTEXT_MESSAGES,
    MAX_PENDING_CONTEXT_TEXT_CHARS,
    MAX_TOOL_RESULT_CHARS,
)
from app.agent.screen_tools import (
    OBSERVE_SCREEN_TOOL_NAME,
    SCREEN_OBSERVATION_REQUEST_ACTION,
)
from app.agent.tool_policy import (
    WINDOWS_CLICK_TOOL_NAME,
    WINDOWS_SCREENSHOT_TOOL_NAME,
    WINDOWS_SNAPSHOT_TOOL_NAME,
)
from app.agent.tools import ToolExecutionResult, ToolRegistry
import app.agent.tool_routing as tool_routing
from app.llm.api_client import ChatMessage, NativeToolCall


def _build_tool_role_message(call: NativeToolCall, result: ToolExecutionResult) -> ChatMessage:
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "name": call.name,
        "content": json.dumps(_redact_tool_result_for_model(result), ensure_ascii=False, default=str),
    }


def _build_tool_messages_for_result(
    call: NativeToolCall,
    result: ToolExecutionResult,
    *,
    include_images: bool,
) -> list[ChatMessage]:
    messages = [_build_tool_role_message(call, result)]
    if include_images:
        image_message = _build_tool_result_image_message([result])
        if image_message is not None:
            messages.append(image_message)
    return messages


def _build_tool_result_image_message(results: list[ToolExecutionResult]) -> ChatMessage | None:
    images = _extract_tool_result_images(results)
    if not images:
        return None
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": "上一个工具结果包含截图，以下图片用于辅助判断页面视觉状态。",
        }
    ]
    content.extend(
        {
            "type": "image_url",
            "image_url": {
                "url": image_url,
                "detail": "low",
            },
        }
        for image_url in images
    )
    return {"role": "user", "content": content}


def _build_skipped_after_pending_messages(
    tool_calls: list[NativeToolCall],
    *,
    start_after_call_id: str,
) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    seen_pending = False
    for call in tool_calls:
        if call.id == start_after_call_id:
            seen_pending = True
            continue
        if not seen_pending:
            continue
        result = ToolExecutionResult(
            tool_name=call.name,
            success=False,
            content={
                "skipped": True,
                "reason": "waiting_for_previous_confirmation",
            },
            error="前一个高风险工具需要对方确认，后续同批工具调用已跳过，请在确认后重新规划。",
        )
        messages.append(_build_tool_role_message(call, result))
    return messages


def _is_screen_observation_request(result: ToolExecutionResult) -> bool:
    if result.tool_name != OBSERVE_SCREEN_TOOL_NAME or not result.success:
        return False
    if not isinstance(result.content, dict):
        return False
    return result.content.get("action") == SCREEN_OBSERVATION_REQUEST_ACTION


def _verify_confirmed_windows_click(
    tools: ToolRegistry,
    tool_name: str,
) -> ToolExecutionResult | None:
    """Windows 桌面点击后追加一次只读截图验证。"""
    if tool_name != WINDOWS_CLICK_TOOL_NAME:
        return None

    screenshot_tool = tools.get(WINDOWS_SCREENSHOT_TOOL_NAME)
    snapshot_tool = tools.get(WINDOWS_SNAPSHOT_TOOL_NAME)

    screenshot_result: ToolExecutionResult | None = None
    if screenshot_tool is not None:
        screenshot_result = tools.execute(WINDOWS_SCREENSHOT_TOOL_NAME, {})
        if screenshot_result.success or snapshot_tool is None:
            return screenshot_result

    if snapshot_tool is not None:
        snapshot_result = tools.execute(
            WINDOWS_SNAPSHOT_TOOL_NAME,
            {
                "use_vision": True,
                "use_ui_tree": False,
            },
        )
        if snapshot_result.success or screenshot_result is None:
            return snapshot_result
        return ToolExecutionResult(
            tool_name="windows__verification",
            success=False,
            content="",
            error=(
                f"Screenshot 验证失败：{screenshot_result.error or '未知错误'}；"
                f"Snapshot 验证失败：{snapshot_result.error or '未知错误'}"
            ),
        )

    return ToolExecutionResult(
        tool_name="windows__verification",
        success=False,
        content="",
        error="没有可用的 windows__Screenshot 或 windows__Snapshot，无法自动验证点击结果。",
    )


def _build_pending_continuation_messages(
    working_messages: list[ChatMessage],
    assistant_message: ChatMessage,
    completed_tool_messages: list[ChatMessage],
    tool_calls: list[NativeToolCall],
    *,
    pending_call_id: str,
) -> list[ChatMessage]:
    """为待确认动作保存原生 tool_calls 上下文，确认后可继续回填 tool role。"""
    messages = [
        *_compact_messages_for_pending_context(working_messages),
        _compact_message_for_pending_context(assistant_message),
        *[
            _compact_message_for_pending_context(message)
            for message in completed_tool_messages
        ],
        *_build_skipped_after_pending_messages(
            tool_calls,
            start_after_call_id=pending_call_id,
        ),
    ]
    return messages[-MAX_PENDING_CONTEXT_MESSAGES:]


def _compact_messages_for_pending_context(messages: list[ChatMessage]) -> list[ChatMessage]:
    return [_compact_message_for_pending_context(message) for message in messages]


def _compact_message_for_pending_context(message: ChatMessage) -> ChatMessage:
    role = message.get("role")
    compacted: ChatMessage = {
        "role": role if isinstance(role, str) and role else "user",
        "content": _compact_pending_context_content(message.get("content")),
    }
    tool_call_id = message.get("tool_call_id")
    if isinstance(tool_call_id, str) and tool_call_id:
        compacted["tool_call_id"] = tool_call_id
    name = message.get("name")
    if isinstance(name, str) and name:
        compacted["name"] = name
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        compacted["tool_calls"] = tool_calls
    return compacted


def _compact_pending_context_content(content: Any) -> str:
    if isinstance(content, str):
        return _truncate_pending_context_text(content)
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text = part.get("text", "")
                parts.append(_truncate_pending_context_text(str(text)))
            elif part.get("type") == "image_url":
                parts.append("[图片内容已省略，确认后继续时请根据文本工具结果判断。]")
        return "\n".join(part for part in parts if part)
    if content is None:
        return ""
    try:
        text = json.dumps(content, ensure_ascii=False, default=str)
    except TypeError:
        text = str(content)
    return _truncate_pending_context_text(text)


def _truncate_pending_context_text(text: str) -> str:
    if len(text) <= MAX_PENDING_CONTEXT_TEXT_CHARS:
        return text
    head_chars = max(1, MAX_PENDING_CONTEXT_TEXT_CHARS // 2)
    tail_chars = MAX_PENDING_CONTEXT_TEXT_CHARS - head_chars
    return (
        text[:head_chars]
        + f"\n...[已省略 {len(text) - head_chars - tail_chars} 字确认上下文]...\n"
        + text[-tail_chars:]
    )


def _build_tool_results_message(
    results: list[ToolExecutionResult],
    include_images: bool = False,
) -> ChatMessage:
    text = _format_tool_results_for_model(results)
    images = _extract_tool_result_images(results) if include_images else []
    if not images:
        return {"role": "user", "content": text}

    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    content.extend(
        {
            "type": "image_url",
            "image_url": {
                "url": image_url,
                "detail": "low",
            },
        }
        for image_url in images
    )
    return {"role": "user", "content": content}


def _build_confirmed_action_result_message(
    action: PendingToolAction,
    results: list[ToolExecutionResult],
) -> ChatMessage:
    text = (
        "对方刚刚确认并执行了一个待确认工具动作。"
        "这不是新的请求，请结合此前上下文继续完成原先想做的事；"
        "如果该动作只是中间步骤，不要把当前窗口状态误当成新问题。\n"
        f"已确认动作：{action.tool_name}\n"
        f"动作参数：{json.dumps(action.arguments, ensure_ascii=False, default=str)}\n"
        f"动作原因：{action.reason or '未提供'}\n\n"
        + _format_tool_results_for_model(results)
    )
    return {"role": "user", "content": text}


def _build_confirmed_action_continuation_rules(action: PendingToolAction) -> str:
    rules = [
        "确认动作续接规则：",
        f"- 对方刚刚确认执行了 {action.tool_name}，这只是前一轮事情的一个中间步骤。",
        "- 不要把工具执行后的界面当成对方发起的新闲聊；必须回到前文原先想做的事继续推进。",
        "- 如果动作成功但事情尚未完成，请继续请求下一步必要工具；如果已经完成，再给最终回复。",
        "- 如果刚打开的是 Windows“运行”窗口，且前文已经计划通过命令完成，应继续输入/提交对应命令，而不是反问对方想用什么工具。",
    ]
    if action.tool_name.startswith("playwright_"):
        rules.append(
            "- 刚确认执行的是 playwright_ 工具，后续网页内点击、输入、读取、截图仍应继续使用 playwright_ 工具；不要因为页面可见就切换到 windows__ 坐标点击。"
        )
    return "\n".join(rules)


def _format_tool_results_for_model(results: list[ToolExecutionResult]) -> str:
    return (
        "工具执行结果如下，请据此给对方最终回复。"
        "如果工具结果标记已附加浏览器截图，请结合截图兜底判断页面内容，不要臆造看不到的信息：\n"
        + json.dumps(
            [_redact_tool_result_for_model(result) for result in results],
            ensure_ascii=False,
            indent=2,
        )
    )


def _redact_tool_result_for_model(result: ToolExecutionResult) -> dict[str, Any]:
    data = result.to_dict()
    content = data.get("content")
    if isinstance(content, str):
        data["content"] = _truncate_text_for_model(content, MAX_TOOL_RESULT_CHARS)
        return data
    if not isinstance(content, dict):
        return data

    # 网页搜索：先解开 MCP 外壳，再保留 digest/长摘要，避免模型只看到空 results。
    if result.tool_name in {"web__web_search", "web_search"}:
        payload = tool_routing.unwrap_mcp_tool_payload(content)
        if not isinstance(payload, dict):
            payload = {}
        rows_in = payload.get("results")
        rows_out: list[dict[str, Any]] = []
        if isinstance(rows_in, list):
            for item in rows_in[:8]:
                if not isinstance(item, dict):
                    continue
                rows_out.append(
                    {
                        "title": item.get("title"),
                        "url": item.get("url"),
                        "snippet": str(item.get("snippet") or "")[:1600],
                    }
                )
        data["content"] = {
            "query": payload.get("query"),
            "source": payload.get("source"),
            "digest": str(payload.get("digest") or "")[:5500],
            "snippet_chars": payload.get("snippet_chars"),
            "results": rows_out,
            "refined_query": payload.get("refined_query"),
            "is_error": bool(content.get("is_error")),
        }
        return data

    if result.tool_name in {"web__fetch_url", "fetch_url", "playwright_get_text"}:
        payload = tool_routing.unwrap_mcp_tool_payload(content)
        if isinstance(payload, dict) and (
            payload.get("text") is not None or payload.get("url") is not None
        ):
            data["content"] = {
                "url": payload.get("url"),
                "title": payload.get("title"),
                "text": str(payload.get("text") or "")[:6000],
                "truncated": payload.get("truncated"),
                "reader": payload.get("reader"),
                "auto_fetched": payload.get("auto_fetched"),
                "is_error": bool(content.get("is_error")),
            }
            return data

    redacted, image_count = _redact_tool_images_from_content(content)
    if image_count:
        redacted["screenshot_attached"] = True
        redacted["screenshot_image_count"] = image_count
    data["content"] = _truncate_value_for_model(redacted, MAX_TOOL_RESULT_CHARS)
    return data


def _truncate_value_for_model(value: Any, max_chars: int) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return value
    head_chars = max(1, max_chars // 2)
    tail_chars = max(0, max_chars - head_chars)
    return {
        "truncated": True,
        "original_chars": len(text),
        "omitted_chars": max(0, len(text) - head_chars - tail_chars),
        "head": text[:head_chars],
        "tail": text[-tail_chars:] if tail_chars else "",
    }


def _truncate_text_for_model(text: str, max_chars: int) -> str | dict[str, Any]:
    if len(text) <= max_chars:
        return text
    head_chars = max(1, max_chars // 2)
    tail_chars = max(0, max_chars - head_chars)
    return {
        "truncated": True,
        "original_chars": len(text),
        "omitted_chars": max(0, len(text) - head_chars - tail_chars),
        "head": text[:head_chars],
        "tail": text[-tail_chars:] if tail_chars else "",
    }


def _extract_tool_result_images(results: list[ToolExecutionResult]) -> list[str]:
    images: list[str] = []
    for result in results:
        if not isinstance(result.content, dict):
            continue
        images.extend(_extract_image_data_urls_from_value(result.content))
    return images[:1]


def _redact_tool_images_from_content(content: dict[str, Any]) -> tuple[dict[str, Any], int]:
    image_count = 0

    def redact(value: Any) -> Any:
        nonlocal image_count
        if isinstance(value, dict):
            if _mcp_image_item_to_data_url(value) is not None:
                image_count += 1
                return {
                    "type": value.get("type", "image"),
                    "image_attached": True,
                    "mime_type": _mcp_image_mime_type(value),
                }
            redacted_dict: dict[str, Any] = {}
            for key, item in value.items():
                if key in {"screenshot_data_url", "mcp_image_data_urls"}:
                    if isinstance(item, str) and item.startswith("data:image/"):
                        image_count += 1
                    elif isinstance(item, list):
                        image_count += len(
                            [
                                image_url
                                for image_url in item
                                if isinstance(image_url, str) and image_url.startswith("data:image/")
                            ]
                        )
                    continue
                redacted_dict[str(key)] = redact(item)
            return redacted_dict
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value

    redacted = redact(content)
    return redacted if isinstance(redacted, dict) else {}, image_count


def _extract_image_data_urls_from_value(value: Any) -> list[str]:
    images: list[str] = []
    if isinstance(value, dict):
        screenshot = value.get("screenshot_data_url")
        if isinstance(screenshot, str) and screenshot.startswith("data:image/"):
            images.append(screenshot)

        mcp_images = value.get("mcp_image_data_urls")
        if isinstance(mcp_images, list):
            images.extend(
                image_url
                for image_url in mcp_images
                if isinstance(image_url, str) and image_url.startswith("data:image/")
            )

        data_url = _mcp_image_item_to_data_url(value)
        if data_url is not None:
            images.append(data_url)

        for item in value.values():
            images.extend(_extract_image_data_urls_from_value(item))
    elif isinstance(value, list):
        for item in value:
            images.extend(_extract_image_data_urls_from_value(item))
    return _deduplicate_preserving_order(images)


def _mcp_image_item_to_data_url(item: dict[str, Any]) -> str | None:
    if str(item.get("type", "")).lower() != "image":
        return None
    data = item.get("data")
    if not isinstance(data, str) or not data.strip():
        return None
    if data.startswith("data:image/"):
        return data
    mime_type = _mcp_image_mime_type(item)
    if not mime_type.startswith("image/"):
        return None
    return f"data:{mime_type};base64,{data}"


def _mcp_image_mime_type(item: dict[str, Any]) -> str:
    mime_type = item.get("mimeType")
    if not isinstance(mime_type, str) or not mime_type.strip():
        mime_type = item.get("mime_type")
    if not isinstance(mime_type, str) or not mime_type.strip():
        mime_type = "image/png"
    return mime_type.strip()


def _deduplicate_preserving_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
