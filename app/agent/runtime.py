from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from app.agent.actions import AgentAction, AgentEvent, AgentResult, MemoryUpdate, PendingToolAction
from app.agent.memory import MemoryStore
from app.agent.screen_tools import (
    OBSERVE_SCREEN_TOOL_NAME,
    SCREEN_OBSERVATION_CAPABILITY,
    SCREEN_OBSERVATION_DISABLED_ERROR,
    SCREEN_OBSERVATION_REQUEST_ACTION,
)
from app.agent.tool_registry import ToolExecutionResult, ToolRegistry
from app.api_client import (
    ApiRequestError,
    ChatMessage,
    OpenAICompatibleClient,
    is_vision_unsupported_error,
    messages_contain_image,
)
from app.chat_reply import ChatReply, parse_chat_reply
from app.debug_log import debug_log, summarize_messages


MAX_TOOL_CALLS_PER_TURN = 3
MAX_TOOL_RESULT_CHARS = 6000


class AgentRuntime:
    """封装聊天决策链路，为后续工具调用和长期记忆留下扩展点。"""

    def __init__(
        self,
        api_client: OpenAICompatibleClient,
        system_prompt: str,
        reply_tones: list[str] | None = None,
        tools: ToolRegistry | None = None,
        memory: MemoryStore | None = None,
    ) -> None:
        self.api_client = api_client
        self.system_prompt = system_prompt
        self.reply_tones = [*reply_tones] if reply_tones is not None else []
        self.tools = tools or ToolRegistry()
        self.memory = memory or MemoryStore()
        self.model_vision_enabled = True

    def update_character(self, system_prompt: str, reply_tones: list[str] | None = None) -> None:
        """角色切换后同步系统提示词和可用语气列表。"""
        self.system_prompt = system_prompt
        self.reply_tones = [*reply_tones] if reply_tones is not None else []

    def set_model_vision_enabled(self, enabled: bool) -> None:
        """允许模型在需要时请求一次当前屏幕截图。"""
        self.model_vision_enabled = enabled

    def handle_user_message(self, messages: list[ChatMessage]) -> AgentResult:
        turn_started_at = time.perf_counter()
        allow_screen_observation = self.model_vision_enabled and not messages_contain_image(messages)
        debug_log(
            "AgentRuntime",
            "开始处理用户消息",
            {
                "message_count": len(messages),
                "allow_screen_observation": allow_screen_observation,
                "model_vision_enabled": self.model_vision_enabled,
                "messages": summarize_messages(messages),
            },
        )
        try:
            planning_started_at = time.perf_counter()
            first_content = self.api_client.complete_raw(
                self._build_tool_planning_prompt(allow_screen_observation=allow_screen_observation),
                messages,
                temperature=0.8,
            )
        except ApiRequestError as exc:
            if messages_contain_image(messages) and is_vision_unsupported_error(exc):
                debug_log("AgentRuntime", "视觉输入不受支持，返回兜底回复", {"error": str(exc)})
                return AgentResult(reply=_build_vision_unsupported_reply())
            raise
        debug_log(
            "AgentRuntime",
            "工具规划模型返回",
            {
                "content": first_content,
                "planning_elapsed_ms": int((time.perf_counter() - planning_started_at) * 1000),
            },
        )
        agent_data = _load_json_object(first_content)
        if agent_data is None:
            debug_log(
                "AgentRuntime",
                "模型返回非 JSON，按普通回复解析",
                {"turn_elapsed_ms": int((time.perf_counter() - turn_started_at) * 1000)},
            )
            return AgentResult(reply=parse_chat_reply(first_content))

        tool_calls = _parse_tool_calls(agent_data.get("tool_calls"))
        debug_log(
            "AgentRuntime",
            "工具规划解析完成",
            {
                "has_reply": isinstance(agent_data.get("reply"), dict),
                "tool_calls": tool_calls,
            },
        )
        if not tool_calls:
            debug_log(
                "AgentRuntime",
                "无需工具调用，直接返回模型回复",
                {"turn_elapsed_ms": int((time.perf_counter() - turn_started_at) * 1000)},
            )
            return AgentResult(reply=_parse_agent_reply(agent_data, first_content))

        execution_results: list[ToolExecutionResult] = []
        pending_actions: list[PendingToolAction] = []
        tools_started_at = time.perf_counter()
        for call in tool_calls[:MAX_TOOL_CALLS_PER_TURN]:
            debug_log("AgentRuntime", "准备工具调用", call)
            prepared = self.tools.prepare_or_execute(
                call["name"],
                call["arguments"],
                call.get("reason", ""),
            )
            if isinstance(prepared, PendingToolAction):
                debug_log("AgentRuntime", "工具调用等待用户确认", prepared.to_dict())
                pending_actions.append(prepared)
            else:
                if _is_screen_observation_request(prepared):
                    if allow_screen_observation:
                        debug_log(
                            "AgentRuntime",
                            "请求屏幕观察 follow-up",
                            {
                                "reason": call.get("reason", ""),
                                "turn_elapsed_ms": int((time.perf_counter() - turn_started_at) * 1000),
                            },
                        )
                        return AgentResult(
                            reply=_build_screen_observation_request_reply(),
                            actions=[
                                AgentAction(
                                    type=SCREEN_OBSERVATION_REQUEST_ACTION,
                                    payload={"reason": call.get("reason", "")},
                                )
                            ],
                        )
                    execution_results.append(
                        ToolExecutionResult(
                            tool_name=OBSERVE_SCREEN_TOOL_NAME,
                            success=False,
                            content="",
                            error=SCREEN_OBSERVATION_DISABLED_ERROR,
                        )
                    )
                    continue
                debug_log("AgentRuntime", "工具调用完成", _redact_tool_result_for_model(prepared))
                execution_results.append(prepared)
        if len(tool_calls) > MAX_TOOL_CALLS_PER_TURN:
            debug_log(
                "AgentRuntime",
                "工具调用数量超过上限",
                {
                    "requested": len(tool_calls),
                    "limit": MAX_TOOL_CALLS_PER_TURN,
                },
            )
            execution_results.append(
                ToolExecutionResult(
                    tool_name="runtime",
                    success=False,
                    content="",
                    error=f"单轮最多执行 {MAX_TOOL_CALLS_PER_TURN} 个工具调用，后续调用已跳过。",
                )
            )

        if pending_actions:
            debug_log(
                "AgentRuntime",
                "返回待确认动作",
                {
                    "pending_actions": [action.to_dict() for action in pending_actions],
                    "tools_elapsed_ms": int((time.perf_counter() - tools_started_at) * 1000),
                    "turn_elapsed_ms": int((time.perf_counter() - turn_started_at) * 1000),
                },
            )
            return AgentResult(
                reply=_build_pending_action_reply(pending_actions),
                actions=[
                    AgentAction(
                        type="pending_action",
                        payload=action.to_dict(),
                    )
                    for action in pending_actions
                ],
                memory_updates=_extract_memory_updates(execution_results),
            )

        try:
            final_started_at = time.perf_counter()
            final_reply = self.api_client.chat(
                self._build_final_reply_prompt(),
                [
                    *messages,
                    {"role": "assistant", "content": first_content},
                    _build_tool_results_message(
                        execution_results,
                        include_images=self.model_vision_enabled,
                    ),
                ],
                self.reply_tones,
            )
        except Exception as exc:
            print(f"[AgentRuntime] 工具结果总结失败，使用本地兜底回复：{exc}")
            debug_log("AgentRuntime", "工具结果总结失败，使用本地兜底回复", {"error": str(exc)})
            final_reply = _build_fallback_tool_reply(execution_results)
        debug_log(
            "AgentRuntime",
            "最终回复生成完成",
            {
                "segments": len(final_reply.segments),
                "actions": [_redact_tool_result_for_model(result) for result in execution_results],
                "tools_elapsed_ms": int((time.perf_counter() - tools_started_at) * 1000),
                "final_reply_elapsed_ms": int((time.perf_counter() - final_started_at) * 1000),
                "turn_elapsed_ms": int((time.perf_counter() - turn_started_at) * 1000),
            },
        )
        return AgentResult(
            reply=final_reply,
            actions=[
                AgentAction(
                    type="tool_call",
                    payload=_redact_tool_result_for_model(result),
                )
                for result in execution_results
            ],
            memory_updates=_extract_memory_updates(execution_results),
        )

    def handle_confirmed_action(self, action: PendingToolAction) -> AgentResult:
        debug_log("AgentRuntime", "执行已确认动作", action.to_dict())
        result = self.tools.execute(action.tool_name, action.arguments)
        try:
            reply = self.api_client.chat(
                self._build_final_reply_prompt(),
                [
                    _build_tool_results_message(
                        [result],
                        include_images=self.model_vision_enabled,
                    )
                ],
                self.reply_tones,
            )
        except Exception as exc:
            print(f"[AgentRuntime] 确认动作总结失败，使用本地兜底回复：{exc}")
            debug_log("AgentRuntime", "确认动作总结失败，使用本地兜底回复", {"error": str(exc)})
            reply = _build_fallback_tool_reply([result])
        debug_log(
            "AgentRuntime",
            "已确认动作处理完成",
            {
                "result": _redact_tool_result_for_model(result),
                "segments": len(reply.segments),
            },
        )
        return AgentResult(
            reply=reply,
            actions=[
                AgentAction(
                    type="tool_call",
                    payload=_redact_tool_result_for_model(result),
                )
            ],
        )

    def handle_cancelled_action(self, action: PendingToolAction) -> AgentResult:
        debug_log("AgentRuntime", "用户取消待确认动作", action.to_dict())
        return AgentResult(
            reply=parse_chat_reply(
                json.dumps(
                    {
                        "segments": [
                            {
                                "ja": "わかった。実行しないでおくね。",
                                "zh": "知道了。我不会执行这个动作。",
                                "tone": "中性",
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            ),
            actions=[
                AgentAction(
                    type="cancelled_action",
                    payload=action.to_dict(),
                )
            ],
        )

    def handle_event(self, event: AgentEvent) -> AgentResult:
        if event.type != "reminder_due":
            return AgentResult(reply=parse_chat_reply("未対応のイベントだよ。"))

        debug_log("AgentRuntime", "处理主动事件", {"event": {"type": event.type, "payload": event.payload}})
        reply = self.api_client.chat(
            self._build_event_reply_prompt(),
            [
                {
                    "role": "user",
                    "content": _format_event_for_model(event),
                }
            ],
            self.reply_tones,
        )
        return AgentResult(
            reply=reply,
            actions=[
                AgentAction(
                    type="event",
                    payload={
                        "event_type": event.type,
                        "event_payload": event.payload,
                    },
                )
            ],
        )

    def _build_tool_planning_prompt(self, allow_screen_observation: bool = False) -> str:
        allowed_capabilities = {SCREEN_OBSERVATION_CAPABILITY} if allow_screen_observation else set()
        tools = self.tools.describe_tools(allowed_capabilities=allowed_capabilities)
        tool_descriptions = json.dumps(
            tools,
            ensure_ascii=False,
            indent=2,
        )
        tones = "、".join(tone for tone in self.reply_tones if tone.strip()) or "中性"
        memory_summary = self._memory_summary()
        current_time = datetime.now().astimezone().isoformat(timespec="seconds")
        screen_observation_instruction = (
            "- observe_screen 只在确实需要当前屏幕内容时调用；如果文字信息已经足够回答，不要截图。"
            if allow_screen_observation
            else "- 当前没有可用的屏幕观察工具；不要请求截图，也不要臆造当前屏幕内容。"
        )
        return f"""
{self.system_prompt.strip()}

你现在可以作为桌面陪伴型 Agent 判断是否需要调用内部工具。
你必须只返回 JSON，不要使用 Markdown 代码块，不要输出额外解释。
如果需要工具，返回 reply 和 tool_calls；如果不需要工具，tool_calls 返回空数组或省略。

长期记忆摘要：
{memory_summary}

当前本地时间：
{current_time}

可用工具：
{tool_descriptions}

返回格式：
{{
  "reply": {{
    "segments": [
      {{"ja": "日文原文", "zh": "中文译文", "tone": "中性"}}
    ]
  }},
  "tool_calls": [
    {{"name": "工具名", "arguments": {{}}, "reason": "为什么需要这个工具"}}
  ]
}}

分段规则：
- 尽量输出 2-4 段文本，每段是一条可以单独显示和朗读的完整小消息，不要把一句话机械切碎。
- 单段建议 35-90 个中文或日文字符；内容需要完整自然，宁可少分段也不要短到像碎片。
- 用户问题包含多个要点、步骤、原因或较长说明时，优先输出 3-4 段，让桌宠可以逐段显示和朗读。
- 如果用户只问很简单的问题，可以只输出 1-2 段。
- 不要因为返回格式示例里只写了一条 segment，就把完整回复固定成一段。

要求：
- tone 只能从这些类别中选择：{tones}。
- ja 中只写夜乃桜要说出口的日文原文，必须是日语，适合直接交给日语 TTS 朗读。
- zh 中只写 ja 对应的自然中文译文，必须是中文。
- 如果工具可以帮助完成用户请求，优先用 tool_calls 表达要执行的动作。
- 不要臆造工具名；只能使用上面列出的工具。
- requires_confirmation 为 true 的工具只会在用户确认后执行；你仍然可以发起 tool_calls，但必须说明原因。
- 需要读取网页内容时，优先使用 browser_get_content；需要打开网页时使用 browser_open_url。
- 需要网页交互时，只能基于当前页面真实内容选择 browser_scroll 或 browser_click，不要臆造 selector 或页面内容。
{screen_observation_instruction}
- 用户说“几分钟后/几秒后/一会儿后”等相对提醒时，add_reminder 必须使用 delay_minutes 或 delay_seconds，不要自己换算 trigger_at。
- 只有用户给出明确日期或钟点时，add_reminder 才使用 trigger_at。
- 不要静默写入长期记忆；只有用户明确要求记住时，才使用 propose_memory_update。
- 只有用户明确确认候选记忆时，才使用 confirm_memory_update。
- 只有用户明确要求忘掉信息时，才使用 forget_memory。
""".strip()

    def _build_final_reply_prompt(self) -> str:
        return f"""
{self.system_prompt.strip()}

你会收到上一轮工具调用结果。请基于这些结果给用户最终回复。
不要再次请求工具，不要提及内部 JSON、工具协议或实现细节。
""".strip()

    def _build_event_reply_prompt(self) -> str:
        tones = "、".join(tone for tone in self.reply_tones if tone.strip()) or "中性"
        return f"""
{self.system_prompt.strip()}

你正在处理 Sakura 桌宠的主动事件。请用角色语气自然提醒用户。
你必须只返回 JSON，不要使用 Markdown 代码块，不要输出额外解释。
JSON 格式如下：
{{"segments":[{{"ja":"日文原文","zh":"中文译文","tone":"提醒"}}]}}

要求：
- tone 只能从这些类别中选择：{tones}。
- ja 中只写夜乃桜要说出口的日文原文，必须是日语，适合直接交给日语 TTS 朗读。
- zh 中只写 ja 对应的自然中文译文，必须是中文。
- 不要提及内部事件类型、JSON 或工具实现。
""".strip()

    def _memory_summary(self) -> str:
        try:
            return self.memory.summary()
        except Exception as exc:
            return f"长期记忆读取失败：{exc}"


def _load_json_object(content: str) -> dict[str, Any] | None:
    text = _strip_code_fence(content.strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _strip_code_fence(content: str) -> str:
    if not content.startswith("```"):
        return content
    lines = content.splitlines()
    if len(lines) >= 3 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return content


def _parse_tool_calls(raw_tool_calls: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_tool_calls, list):
        return []

    tool_calls: list[dict[str, Any]] = []
    for item in raw_tool_calls:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        arguments = item.get("arguments", {})
        reason = item.get("reason", "")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(arguments, dict):
            arguments = {}
        if not isinstance(reason, str):
            reason = ""
        tool_calls.append({"name": name.strip(), "arguments": arguments, "reason": reason.strip()})
    return tool_calls


def _parse_agent_reply(agent_data: dict[str, Any], fallback_content: str) -> ChatReply:
    reply_data = agent_data.get("reply")
    if isinstance(reply_data, dict):
        return parse_chat_reply(json.dumps(reply_data, ensure_ascii=False))
    return parse_chat_reply(fallback_content)


def _is_screen_observation_request(result: ToolExecutionResult) -> bool:
    if result.tool_name != OBSERVE_SCREEN_TOOL_NAME or not result.success:
        return False
    if not isinstance(result.content, dict):
        return False
    return result.content.get("action") == SCREEN_OBSERVATION_REQUEST_ACTION


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


def _format_tool_results_for_model(results: list[ToolExecutionResult]) -> str:
    return (
        "工具执行结果如下，请据此给用户最终回复。"
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

    redacted = dict(content)
    if redacted.pop("screenshot_data_url", None):
        redacted["screenshot_attached"] = True
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
        image_url = result.content.get("screenshot_data_url")
        if isinstance(image_url, str) and image_url.startswith("data:image/"):
            images.append(image_url)
    return images[:1]


def _build_pending_action_reply(actions: list[PendingToolAction]) -> ChatReply:
    if len(actions) == 1:
        action = actions[0]
        text = _describe_pending_action(action)
        return parse_chat_reply(
            json.dumps(
                {
                    "segments": [
                        {
                            "ja": "実行する前に確認させて。",
                            "zh": f"执行前需要你确认：{text}",
                            "tone": "提醒",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        )

    return parse_chat_reply(
        json.dumps(
            {
                "segments": [
                    {
                        "ja": "いくつか確認が必要な操作があるよ。",
                        "zh": f"有 {len(actions)} 个动作需要你确认，我会先处理第一个。",
                        "tone": "提醒",
                    }
                ]
            },
            ensure_ascii=False,
        )
    )


def _describe_pending_action(action: PendingToolAction) -> str:
    if action.tool_name == "open_url":
        return f"打开网页 {action.arguments.get('url', '')}"
    if action.tool_name == "browser_open_url":
        return f"在受控浏览器中打开网页 {action.arguments.get('url', '')}"
    if action.tool_name == "browser_scroll":
        direction = action.arguments.get("direction", "")
        amount = action.arguments.get("amount", "")
        return f"滚动受控浏览器页面 {direction} {amount}"
    if action.tool_name == "browser_click":
        return f"点击受控浏览器页面元素 {action.arguments.get('selector', '')}"
    if action.tool_name == "open_local_folder":
        return f"打开文件夹 {action.arguments.get('path', '')}"
    return f"执行 {action.tool_name}"


def _build_screen_observation_request_reply() -> ChatReply:
    return parse_chat_reply(
        json.dumps(
            {
                "segments": [
                    {
                        "ja": "画面を確認してから答えるね。",
                        "zh": "我先看一下当前画面再回答。",
                        "tone": "提醒",
                    }
                ]
            },
            ensure_ascii=False,
        )
    )


def _build_fallback_tool_reply(results: list[ToolExecutionResult]) -> ChatReply:
    if not results:
        return parse_chat_reply("ツール結果の確認に失敗したよ。")

    succeeded = [result for result in results if result.success]
    failed = [result for result in results if not result.success]
    if succeeded and not failed:
        summary = _summarize_tool_results(succeeded)
        return parse_chat_reply(
            json.dumps(
                {
                    "segments": [
                        {
                            "ja": f"処理は終わったよ。{summary}",
                            "zh": f"已经处理好了。{summary}",
                            "tone": "提醒",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        )

    error_text = "；".join(
        f"{result.tool_name}: {result.error or '执行失败'}"
        for result in failed
    )
    return parse_chat_reply(
        json.dumps(
            {
                "segments": [
                    {
                        "ja": "処理中に問題が起きたみたい。設定かネットワークを確認して。",
                        "zh": f"工具执行时出了点问题：{error_text}",
                        "tone": "困惑",
                    }
                ]
            },
            ensure_ascii=False,
        )
    )


def _build_vision_unsupported_reply() -> ChatReply:
    return parse_chat_reply(
        json.dumps(
            {
                "segments": [
                    {
                        "ja": "今のモデルでは画像を見られないみたい。画面の内容は勝手に想像しないでおくね。",
                        "zh": "当前模型或接口似乎不支持图片输入。我不会猜屏幕内容，请换成支持视觉的模型后再试。",
                        "tone": "困惑",
                    }
                ]
            },
            ensure_ascii=False,
        )
    )


def _summarize_tool_results(results: list[ToolExecutionResult]) -> str:
    parts: list[str] = []
    for result in results:
        if isinstance(result.content, dict):
            if isinstance(result.content.get("reminder"), dict):
                reminder = result.content["reminder"]
                text = reminder.get("text", "")
                trigger_at = reminder.get("trigger_at", "")
                parts.append(f"提醒「{text}」已设置在 {trigger_at}。")
            elif isinstance(result.content.get("task"), dict):
                task = result.content["task"]
                parts.append(f"待办「{task.get('text', '')}」已更新。")
            elif isinstance(result.content.get("pending_update"), dict):
                update = result.content["pending_update"]
                parts.append(f"候选记忆「{update.get('content', '')}」已记录，等待确认。")
            elif isinstance(result.content.get("memory"), dict):
                memory = result.content["memory"]
                parts.append(f"记忆「{memory.get('content', '')}」已确认。")
            elif result.tool_name == "open_url":
                parts.append(f"网页已打开：{result.content.get('url', '')}。")
            elif result.tool_name == "browser_open_url":
                parts.append(f"受控浏览器已打开：{result.content.get('url', '')}。")
            elif result.tool_name == "browser_get_content":
                title = result.content.get("title", "")
                text = result.content.get("text", "")
                parts.append(f"网页内容已读取：{title}。{str(text)[:120]}")
            elif result.tool_name == "browser_scroll":
                parts.append(f"页面已滚动到 Y={result.content.get('scroll_y', '')}。")
            elif result.tool_name == "browser_click":
                parts.append(f"页面元素已点击：{result.content.get('selector', '')}。")
            elif result.tool_name == "browser_get_state":
                parts.append(
                    f"当前网页：{result.content.get('title', '')}，"
                    f"滚动位置 Y={result.content.get('scroll_y', '')}。"
                )
            elif result.tool_name == "open_local_folder":
                parts.append(f"文件夹已打开：{result.content.get('path', '')}。")
            elif result.tool_name == "read_note":
                parts.append(f"笔记「{result.content.get('name', '')}」已读取。")
            elif result.tool_name == "write_note":
                parts.append(f"笔记「{result.content.get('name', '')}」已保存。")
            else:
                parts.append(f"{result.tool_name} 已完成。")
        else:
            parts.append(f"{result.tool_name} 已完成。")
    return " ".join(part for part in parts if part).strip()


def _format_event_for_model(event: AgentEvent) -> str:
    return (
        "主动事件如下，请生成要直接说给用户听的提醒：\n"
        + json.dumps(
            {
                "type": event.type,
                "payload": event.payload,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _extract_memory_updates(results: list[ToolExecutionResult]) -> list[MemoryUpdate]:
    updates: list[MemoryUpdate] = []
    for result in results:
        if result.tool_name != "propose_memory_update" or not result.success:
            continue
        if not isinstance(result.content, dict):
            continue
        raw_update = result.content.get("pending_update")
        if not isinstance(raw_update, dict):
            continue
        update_id = raw_update.get("id")
        category = raw_update.get("category")
        content = raw_update.get("content")
        reason = raw_update.get("reason", "")
        if not all(isinstance(value, str) and value.strip() for value in (update_id, category, content)):
            continue
        updates.append(
            MemoryUpdate(
                id=update_id.strip(),
                category=category.strip(),
                content=content.strip(),
                reason=reason.strip() if isinstance(reason, str) else "",
            )
        )
    return updates
