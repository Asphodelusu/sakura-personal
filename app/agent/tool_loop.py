"""工具循环核心 — 从 runtime.py 拆分的 mixin。

AgentRuntime 的主对话 / 工具调用 / 事件处理循环。
消费 self 上由 AgentRuntime.__init__ 设置的状态；跨 mixin 调用经 MRO 解析。
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from typing import Any, Callable

import app.agent.tool_routing as tool_routing
from app.agent.actions import AgentAction, AgentEvent, AgentProgress, AgentResult, PendingToolAction
from app.agent.context_orchestrator import build_context_request
from app.agent.event_message_builder import _build_event_messages
from app.agent.fallback_replies import (
    _build_fallback_tool_reply,
    _build_pending_action_reply,
    _build_proactive_vision_unsupported_reply,
    _build_screen_observation_request_reply,
    _build_vision_unsupported_reply,
)
from app.agent.progress_emitter import (
    _build_stream_progress_emitter,
    _emit_progress_from_content,
    _emit_progress_reply,
    _should_emit_progress,
)
from app.agent.screen_tools import (
    OBSERVE_SCREEN_TOOL_NAME,
    SCREEN_OBSERVATION_CAPABILITY,
    SCREEN_OBSERVATION_DISABLED_ERROR,
    SCREEN_OBSERVATION_REQUEST_ACTION,
)
from app.agent.tool_call_utils import (
    _assistant_turn_message,
    _build_duplicate_tool_call_result,
    _groups_from_search_tools_result,
    _is_duplicate_tool_call,
    _native_tool_call_to_policy_call,
    _tool_arguments_for_execution,
    _tool_call_reason,
    _try_supplement_missed_memory_tools,
)
from app.agent.tool_message_builder import (
    _build_confirmed_action_continuation_rules,
    _build_confirmed_action_result_message,
    _build_pending_continuation_messages,
    _build_skipped_after_pending_messages,
    _build_tool_messages_for_result,
    _build_tool_role_message,
    _build_tool_result_image_message,
    _is_screen_observation_request,
    _redact_tool_result_for_model,
    _verify_confirmed_windows_click,
)
from app.agent.tool_policy import (
    BROWSER_NAVIGATE_TOOL_NAME,
    BROWSER_SNAPSHOT_TOOL_NAME,
    WINDOWS_CLICK_TOOL_NAME,
    WINDOWS_SCREENSHOT_TOOL_NAME,
    WINDOWS_SNAPSHOT_TOOL_NAME,
)
from app.agent.tools import ToolExecutionResult, ToolRegistry
from app.agent.turn_classifier import classify_turn_depth
from app.agent.turn_routing import (
    RecallDecision,
    TurnPlan,
    TurnRoutingSettings,
    TurnState,
    resolve_recall_decision,
    resolve_turn_plan,
    should_invoke_turn_classifier,
)
from app.agent.web_evidence import (
    _build_web_search_evidence_packet_message,
    _latest_user_text,
    _message_text_content,
    _turn_had_successful_web_search,
    _working_messages_have_web_search_evidence,
)
from app.core.cancellation import CancelChecker, OperationCancelled, check_cancelled
from app.core.debug_log import debug_log, debug_body_enabled, summarize_messages
from app.llm.api_client import (
    ApiRequestError,
    ChatCompletionTurn,
    ChatMessage,
    NativeToolCall,
    STRUCTURED_JSON_RESPONSE_FORMAT,
    is_vision_unsupported_error,
    messages_contain_image,
    strip_image_parts_from_messages,
)
from app.llm.chat_reply import ChatReply, ChatSegment, parse_chat_reply, parse_chat_reply_result
from app.llm.context_trimming import trim_messages_for_model
from app.llm.prompts.types import ContextFragment



def _trim_working_messages_for_model(working_messages: list[ChatMessage]) -> None:
    """工具循环内按 token 预算裁剪历史，避免长对话拖慢每次 API。"""
    trimmed = trim_messages_for_model(working_messages)
    if len(trimmed) >= len(working_messages):
        return
    debug_log(
        "AgentRuntime",
        "裁剪入模历史",
        {"before": len(working_messages), "after": len(trimmed)},
    )
    working_messages[:] = trimmed


def _auto_tool_call_entry(call_id: str, name: str, arguments_json: str) -> dict[str, Any]:
    """构造 auto 工具调用的 assistant tool_call 条目，供 _extend_assistant_with_tool_calls 使用。"""
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments_json,
        },
    }


def _extend_assistant_with_tool_calls(
    turn_message: dict[str, Any],
    extra_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    """把系统自动补充的工具调用声明进 assistant 消息的 tool_calls。

    原因：sanitize_tool_conversation_messages 只保留「assistant 消息 tool_calls 里
    声明过的 id」对应的 tool 消息。auto snapshot / refine / fetch 不是模型发起的，
    turn.message 里没有它们的 id，结果会被丢弃。这里把额外 tool_call 条目并入
    assistant 消息的 tool_calls，使 sanitize 能识别。
    """
    if not extra_calls:
        return turn_message
    extended = dict(turn_message)
    existing = list(extended.get("tool_calls") or [])
    extended["tool_calls"] = existing + list(extra_calls)
    return extended


def _build_debug_meta(
    api_client: Any,
    execution_results: list,
    total_tool_calls: int,
    turn_started_at: float,
    prompt_inspection: dict[str, Any] | None = None,
    turn_state: TurnState | None = None,
) -> dict[str, Any]:
    """构建写入聊天记录的调试元数据，包含工具调用摘要和耗时。"""
    meta: dict[str, Any] = {
        "model": getattr(api_client, "model", getattr(api_client, "model_name", "unknown")),
        "turn_elapsed_ms": int((time.perf_counter() - turn_started_at) * 1000),
        "tool_calls_total": total_tool_calls,
        "tool_results": [
            {
                "name": result.tool_name,
                "success": result.success,
                "error": result.error or "",
            }
            for result in execution_results
        ],
        "prompt_inspection": prompt_inspection,
    }
    if turn_state is not None:
        meta["turn_routing"] = {
            "recall_decision": turn_state.recall_decision,
            "tier": turn_state.turn_plan.tier,
            "modality": turn_state.turn_plan.modality,
            "client_key": turn_state.turn_plan.client_key,
            "decided_by": turn_state.turn_plan.decided_by,
        }
    return meta


class AgentRuntimeToolLoopMixin:
    def handle_user_message(
        self,
        messages: list[ChatMessage],
        progress_callback: ProgressCallback | None = None,
        cancel_checker: CancelChecker | None = None,
    ) -> AgentResult:
        check_cancelled(cancel_checker)
        turn_started_at = time.perf_counter()
        allow_screen_observation = (
            self.model_vision_enabled
            and self.autonomous_screen_observation_enabled
            and not messages_contain_image(messages)
            and tool_routing._should_offer_screen_observation(messages)
        )
        debug_log(
            "AgentRuntime",
            "开始处理用户消息",
            {
                "message_count": len(messages),
                "allow_screen_observation": allow_screen_observation,
                "model_vision_enabled": self.model_vision_enabled,
                "autonomous_screen_observation_enabled": self.autonomous_screen_observation_enabled,
                "messages": summarize_messages(messages),
            },
        )
        return self._run_tool_loop(
            messages,
            allow_screen_observation=allow_screen_observation,
            turn_started_at=turn_started_at,
            vision_unsupported_reply=_build_vision_unsupported_reply(),
            progress_callback=progress_callback,
            cancel_checker=cancel_checker,
        )


    def _run_tool_loop(
        self,
        messages: list[ChatMessage],
        *,
        allow_screen_observation: bool,
        turn_started_at: float,
        proactive_mode: bool = False,
        context_source: str = "chat",
        event_type: str = "",
        event_payload: dict[str, Any] | None = None,
        planning_extra_instructions: str = "",
        initial_actions: list[AgentAction] | None = None,
        vision_unsupported_reply: ChatReply | None = None,
        progress_callback: ProgressCallback | None = None,
        cancel_checker: CancelChecker | None = None,
    ) -> AgentResult:
        """执行 OpenAI 原生 tools/tool_calls 循环。"""
        working_messages: list[ChatMessage] = [*messages]
        original_current_input = _latest_user_text(working_messages)
        execution_results: list[ToolExecutionResult] = []
        emitted_actions: list[AgentAction] = [*(initial_actions or [])]
        total_tool_calls = 0
        self._inner_thought_done_for_turn = False
        self._turn_interest = None
        self._turn_verbosity_guidance = ""
        active_groups: set[str] = tool_routing.infer_active_tool_groups_from_messages(working_messages)
        debug_log(
            "AgentRuntime",
            "本轮初始可见工具组",
            {"active_groups": sorted(active_groups)},
        )
        turn_memory_fragments = ()
        memory_status = "unknown"
        memory_needs_refresh = True
        turn_state: TurnState | None = None
        web_search_nudge_sent = False
        memory_tool_result_cache: dict[tuple[str, tuple[str, ...]], ToolExecutionResult] = {}
        # 本轮系统自动补充的工具调用（auto snapshot / refine / fetch）。
        # 它们不是模型在 assistant 消息里声明的，需在 append 前把对应的
        # tool_call 条目并入 assistant 消息，否则 sanitize_tool_conversation_messages
        # 会因「无匹配 assistant tool_call」而丢弃其结果。
        auto_tool_calls: list[dict[str, Any]] = []
        last_turn: ChatCompletionTurn | None = None
        # 每轮记录用户情绪（之前只在 build_memory_context 内触发，defer/light 时被跳过）
        user_text = _latest_user_text(working_messages)
        if user_text:
            try:
                self.memory.record_user_emotion(user_text)
            except Exception as exc:
                debug_log(
                    "AgentRuntime",
                    "记录用户情绪失败",
                    {"error": str(exc)},
                )
        loop_settings = self.runtime_loop_settings
        for step_index in range(loop_settings.max_agent_steps_per_turn):
            check_cancelled(cancel_checker)
            _trim_working_messages_for_model(working_messages)
            browser_page_mode = tool_routing._should_prefer_browser_page_tools(working_messages)
            browser_page_guard_active = (
                browser_page_mode
                and tool_routing._browser_dom_tools_available(self.tools)
                and not tool_routing._recent_browser_tool_failed(working_messages)
                and not tool_routing._latest_user_explicitly_requests_windows_control(working_messages)
            )
            visible_browser_guard_active = (
                tool_routing._latest_user_requests_visible_browser(working_messages)
                and tool_routing._browser_dom_tools_available(self.tools)
            )
            if browser_page_mode or visible_browser_guard_active:
                active_groups.add("browser")
            allowed_capabilities = {SCREEN_OBSERVATION_CAPABILITY} if allow_screen_observation else set()
            tool_defs = tool_routing._filter_openai_tools_for_browser_routing(
                self.tools.describe_openai_tools(
                    allowed_capabilities=allowed_capabilities,
                    active_groups=active_groups,
                ),
                browser_page_mode=browser_page_guard_active,
                visible_browser_mode=visible_browser_guard_active,
            )
            try:
                planning_started_at = time.perf_counter()
                tool_names = [
                    str(item.get("function", {}).get("name", ""))
                    for item in tool_defs
                    if isinstance(item, dict) and isinstance(item.get("function"), dict)
                ]
                request = build_context_request(
                    working_messages,
                    source=context_source,
                    mode="proactive" if proactive_mode else "normal",
                    event_type=event_type,
                    step_index=step_index,
                    remaining_steps=loop_settings.max_agent_steps_per_turn - step_index - 1,
                    available_tools=tool_names,
                    event_payload=self._enrich_event_payload(event_payload),
                    service_status={"memory": memory_status},
                    current_input=original_current_input,
                )
                if step_index == 0:
                    turn_state = self._resolve_turn_state(
                        working_messages,
                        request,
                        proactive_mode=proactive_mode,
                    )
                assert turn_state is not None
                if turn_state.turn_plan.suppress_generation:
                    debug_log(
                        "AgentRuntime",
                        "Turn 已被路由层抑制，不调用模型",
                        {"decided_by": turn_state.turn_plan.decided_by},
                    )
                    return AgentResult(reply=ChatReply(segments=[]), actions=emitted_actions)
                # step0：Flash 独白 ∥ 记忆改写/召回 fork-join（先 resolve turn_state；join 后再拼 prompt）
                thought_launch = None
                recall_launch = None
                if step_index == 0:
                    thought_launch = self._launch_inner_thought_worker(
                        working_messages,
                        turn_state,
                        proactive_mode=proactive_mode,
                    )
                try:
                    intimacy_focus = self._intimacy_focus_active()
                    if memory_needs_refresh:
                        if intimacy_focus:
                            # 亲密模式：轻量语义召回（当下亲密相关记忆可进；不做全量/渐进索引）
                            light_recall = self.memory_recall.recall(
                                request, light_mode=True,
                            )
                            turn_memory_fragments = tuple(light_recall.fragments)
                            memory_status = "rhythm_light"
                        elif turn_state.recall_decision == "recall":
                            # 与独白并行：改写 + 向量检索在独立线程
                            if step_index == 0:
                                recall_launch = self._launch_memory_recall_worker(
                                    request, light_mode=False,
                                )
                                recall = self._finalize_memory_recall_worker(recall_launch)
                                recall_launch = None
                                if recall is None:
                                    recall = self.memory_recall.recall(request)
                            else:
                                recall = self.memory_recall.recall(request)
                            turn_memory_fragments = list(recall.fragments)
                            memory_status = recall.status
                            # 渐进检索：追加以往记忆的标题索引 + 工具提示
                            if self._progressive_memory:
                                progressive = self._build_progressive_index_fragment(
                                    request.current_input,
                                    recall.fragments,
                                )
                                if progressive:
                                    turn_memory_fragments.append(progressive)
                            turn_memory_fragments = tuple(turn_memory_fragments)
                        elif turn_state.recall_decision == "light":
                            # 轻量召回：连续性上下文 + 1-2 条相关情节记忆（与独白并行）
                            if step_index == 0:
                                recall_launch = self._launch_memory_recall_worker(
                                    request, light_mode=True,
                                )
                                light_recall = self._finalize_memory_recall_worker(
                                    recall_launch,
                                )
                                recall_launch = None
                                if light_recall is None:
                                    light_recall = self.memory_recall.recall(
                                        request, light_mode=True,
                                    )
                            else:
                                light_recall = self.memory_recall.recall(
                                    request, light_mode=True,
                                )
                            continuity = self.memory.build_continuity_context()
                            combined = list(light_recall.fragments)
                            if continuity:
                                combined.insert(
                                    0,
                                    ContextFragment(
                                        fragment_id="memory.continuity",
                                        source="memory",
                                        content=continuity,
                                        trust="trusted",
                                        priority=90,
                                        token_budget=600,
                                        sensitivity="private",
                                        cache_scope="turn",
                                        required=True,
                                    ),
                                )
                            turn_memory_fragments = tuple(combined)
                            memory_status = "light"
                        else:
                            # defer/skip：仅注入连续性上下文（心情+关系快照）；勿死等独白
                            continuity = self.memory.build_continuity_context()
                            if continuity:
                                turn_memory_fragments = (
                                    ContextFragment(
                                        fragment_id="memory.continuity",
                                        source="memory",
                                        content=continuity,
                                        trust="trusted",
                                        priority=90,
                                        token_budget=600,
                                        sensitivity="private",
                                        cache_scope="turn",
                                        required=True,
                                    ),
                                )
                            else:
                                turn_memory_fragments = ()
                            memory_status = (
                                "skipped"
                                if turn_state.recall_decision == "skip"
                                else "deferred"
                            )
                        memory_needs_refresh = False
                        request = replace(request, service_status={"memory": memory_status})
                    # 同轮中途开启亲密模式：从全量/日常召回切到轻量语义召回
                    if intimacy_focus and memory_status != "rhythm_light":
                        light_recall = self.memory_recall.recall(
                            request, light_mode=True,
                        )
                        turn_memory_fragments = tuple(light_recall.fragments)
                        memory_status = "rhythm_light"
                        request = replace(request, service_status={"memory": memory_status})
                finally:
                    # 召回抛错也要回收线程；窗口写入仅发生在此处（主线程）
                    if recall_launch is not None:
                        self._finalize_memory_recall_worker(recall_launch)
                    self._finalize_inner_thought_worker(thought_launch)
                snapshot = self.context_orchestrator.build_snapshot(
                    request,
                    providers=self.context_providers,
                    session_fragments=self._session_state_fragments(request),
                    memory_fragments=turn_memory_fragments,
                )
                prompt_build = (
                    self._build_proactive_tool_prompt_result(
                        snapshot,
                        extra_instructions=planning_extra_instructions,
                    )
                    if proactive_mode
                    else self._build_tool_prompt_result(
                        snapshot,
                        allow_screen_observation=allow_screen_observation,
                        extra_instructions=planning_extra_instructions,
                        browser_page_mode=browser_page_guard_active,
                        visible_browser_mode=visible_browser_guard_active,
                        recent_messages=working_messages,
                    )
                )
                self._record_prompt_inspection(prompt_build.inspection)
                turn_client = self._client_for_turn(working_messages, turn_state.turn_plan)
                dialogue_temperature, dialogue_extra_params = self._resolve_dialogue_params_for_turn(
                    turn_state.turn_plan,
                    turn_client,
                )
                has_tool_defs = bool(tool_defs)
                turn = turn_client.complete_with_tools(
                    prompt_build.system_prompt,
                    working_messages,
                    tools=tool_defs,
                    tool_choice="auto",
                    temperature=dialogue_temperature,
                    runtime_context=prompt_build.runtime_context,
                    structured_response=(
                        not has_tool_defs
                        and not messages_contain_image(working_messages)
                    ),
                    cancel_checker=cancel_checker,
                    request_purpose="initial" if step_index == 0 else "tool_step",
                    **dialogue_extra_params,
                )
                last_turn = turn
                if turn.runtime_context_role != prompt_build.inspection.runtime_role:
                    self._record_prompt_inspection(
                        replace(prompt_build.inspection, runtime_role=turn.runtime_context_role)
                    )
            except ApiRequestError as exc:
                if messages_contain_image(working_messages) and is_vision_unsupported_error(exc):
                    debug_log("AgentRuntime", "视觉输入不受支持，返回兜底回复", {"error": str(exc)})
                    return AgentResult(
                        reply=vision_unsupported_reply or _build_vision_unsupported_reply(),
                        actions=emitted_actions,
                    )
                raise
            check_cancelled(cancel_checker)
            debug_log(
                "AgentRuntime",
                "原生工具模型返回",
                {
                    "step_index": step_index,
                    "content": turn.content,
                    "tool_calls": [
                        {"id": call.id, "name": call.name, "arguments": call.arguments}
                        for call in turn.tool_calls
                    ],
                    "planning_elapsed_ms": int((time.perf_counter() - planning_started_at) * 1000),
                },
            )
            if not turn.tool_calls:
                supplement = _try_supplement_missed_memory_tools(
                    tools=self.tools,
                    working_messages=working_messages,
                    turn=turn,
                    execution_results=execution_results,
                    step_index=step_index,
                    model_vision_enabled=self.model_vision_enabled,
                )
                if supplement is not None:
                    execution_results.extend(supplement.results)
                    total_tool_calls += len(supplement.results)
                    emitted_actions.extend(
                        AgentAction(
                            type="tool_call",
                            payload=_redact_tool_result_for_model(result),
                        )
                        for result in supplement.results
                    )
                    if any(
                        result.tool_name in {"memory_remember", "memory_forget"}
                        for result in supplement.results
                    ):
                        memory_needs_refresh = True
                    if supplement.continue_loop:
                        working_messages.extend(supplement.appended_messages)
                        continue
                # 检测：需要联网却未真正 web_search（嘴上说要查 / 用户明确要查网却只 search_tools 或空跑）。
                # 每轮最多催一次，避免死循环。
                if (
                    supplement is None
                    and not web_search_nudge_sent
                    and self.tools.get("web__web_search") is not None
                    and not _turn_had_successful_web_search(execution_results)
                    and (
                        tool_routing.assistant_intends_web_search(turn.content)
                        or tool_routing.user_message_needs_web_lookup(working_messages)
                    )
                ):
                    working_messages.append(_assistant_turn_message(turn))
                    working_messages.append(
                        {
                            "role": "user",
                            "content": (
                                "（注意：本轮需要联网查询时，请现在使用 web__web_search 实际查询，"
                                "不要只 search_tools 或口头说查不到。查询天气/新闻等实时信息同理。）"
                            ),
                        }
                    )
                    active_groups.add("mcp")
                    web_search_nudge_sent = True
                    debug_log(
                        "AgentRuntime",
                        "需要联网查询但未调用 web__web_search，补激活 mcp 并继续循环",
                        {
                            "step_index": step_index,
                            "content_preview": turn.content[:200],
                            "active_groups": sorted(active_groups),
                        },
                    )
                    continue
                debug_log(
                    "AgentRuntime",
                    "多步循环完成，返回模型回复",
                    {
                        "step_index": step_index,
                        "tool_result_count": len(execution_results),
                        "turn_elapsed_ms": int((time.perf_counter() - turn_started_at) * 1000),
                    },
                )
                parsed = self._parse_final_reply_with_retry(
                    prompt_build.system_prompt,
                    working_messages,
                    self._resolve_final_reply_content(
                        turn.content,
                        system_prompt=prompt_build.system_prompt,
                        working_messages=working_messages,
                        runtime_context=prompt_build.runtime_context,
                        cancel_checker=cancel_checker,
                        turn_state=turn_state,
                    ),
                    runtime_context=prompt_build.runtime_context,
                    cancel_checker=cancel_checker,
                    turn_state=turn_state,
                )
                return AgentResult(
                    reply=self._normalize_reply(self._seal_reply_tones(parsed.reply)),
                    _debug=_build_debug_meta(
                        turn_client, execution_results,
                        total_tool_calls, turn_started_at,
                        self.get_last_prompt_inspection(),
                        turn_state=turn_state,
                    ),
                    actions=emitted_actions,
                )

            web_planned = any(
                call.name in {"web__web_search", "web_search", "web__fetch_url", "fetch_url"}
                for call in turn.tool_calls
            )
            if web_planned and step_index == 0 and not (turn.content or "").strip():
                _emit_progress_reply(
                    progress_callback,
                    ja="ちょっと調べてみるね。",
                    zh="我查查。",
                    stage="web_planning",
                    metadata={
                        "step_index": step_index,
                        "tool_names": [call.name for call in turn.tool_calls],
                    },
                    cancel_checker=cancel_checker,
                )

            _emit_progress_from_content(
                progress_callback,
                turn.content,
                stage="tool_planning",
                metadata={
                    "step_index": step_index,
                    "tool_names": [call.name for call in turn.tool_calls],
                    "tool_call_count": len(turn.tool_calls),
                },
                cancel_checker=cancel_checker,
            )
            step_results: list[ToolExecutionResult] = []
            pending_actions: list[PendingToolAction] = []
            tool_messages: list[ChatMessage] = []
            tools_started_at = time.perf_counter()
            should_fast_forward_final_reply = False
            allowed_calls = min(
                len(turn.tool_calls),
                loop_settings.max_tool_calls_per_step,
                max(0, loop_settings.max_tool_calls_per_turn - total_tool_calls),
            )
            for call in turn.tool_calls[:allowed_calls]:
                check_cancelled(cancel_checker)
                execution_arguments = _tool_arguments_for_execution(call, self.tools)
                call_data = _native_tool_call_to_policy_call(call, execution_arguments)
                debug_log("AgentRuntime", "准备工具调用", {"step_index": step_index, **call_data})
                if _is_duplicate_tool_call(call, execution_results):
                    duplicate_result = _build_duplicate_tool_call_result(call)
                    debug_log("AgentRuntime", "跳过重复工具调用", duplicate_result.to_dict())
                    step_results.append(duplicate_result)
                    execution_results.append(duplicate_result)
                    tool_messages.extend(
                        _build_tool_messages_for_result(
                            call,
                            duplicate_result,
                            include_images=self.model_vision_enabled,
                        )
                    )
                    emitted_actions.append(
                        AgentAction(
                            type="tool_call",
                            payload=_redact_tool_result_for_model(duplicate_result),
                        )
                    )
                    continue
                memory_gate = tool_routing.resolve_memory_search_gate(
                    tool_name=call.name,
                    arguments=execution_arguments,
                    execution_results=execution_results,
                    recall_decision=turn_state.recall_decision,
                    result_cache=memory_tool_result_cache,
                )
                if memory_gate is None:
                    memory_gate = tool_routing.resolve_memory_detail_gate(
                        tool_name=call.name,
                        arguments=execution_arguments,
                        result_cache=memory_tool_result_cache,
                    )
                if memory_gate is not None:
                    debug_log(
                        "AgentRuntime",
                        "记忆工具闸门短路",
                        {
                            "tool_name": call.name,
                            "reason": (
                                memory_gate.content.get("reason")
                                if isinstance(memory_gate.content, dict)
                                else ""
                            ),
                        },
                    )
                    step_results.append(memory_gate)
                    execution_results.append(memory_gate)
                    tool_messages.extend(
                        _build_tool_messages_for_result(
                            call,
                            memory_gate,
                            include_images=self.model_vision_enabled,
                        )
                    )
                    emitted_actions.append(
                        AgentAction(
                            type="tool_call",
                            payload=_redact_tool_result_for_model(memory_gate),
                        )
                    )
                    continue
                total_tool_calls += 1
                if tool_routing._should_block_windows_tool_for_browser_page(call_data, browser_page_guard_active):
                    blocked_result = tool_routing._build_browser_page_windows_tool_block_result(call_data)
                    debug_log("AgentRuntime", "浏览器页面模式拦截 Windows 工具", blocked_result.to_dict())
                    step_results.append(blocked_result)
                    execution_results.append(blocked_result)
                    tool_messages.extend(
                        _build_tool_messages_for_result(
                            call,
                            blocked_result,
                            include_images=self.model_vision_enabled,
                        )
                    )
                    emitted_actions.append(
                        AgentAction(
                            type="tool_call",
                            payload=_redact_tool_result_for_model(blocked_result),
                        )
                    )
                    continue
                if tool_routing._should_block_background_web_tool_for_visible_browser(call_data, visible_browser_guard_active):
                    blocked_result = tool_routing._build_visible_browser_web_tool_block_result(call_data)
                    debug_log("AgentRuntime", "可见浏览器模式拦截后台网页工具", blocked_result.to_dict())
                    step_results.append(blocked_result)
                    execution_results.append(blocked_result)
                    tool_messages.extend(
                        _build_tool_messages_for_result(
                            call,
                            blocked_result,
                            include_images=self.model_vision_enabled,
                        )
                    )
                    emitted_actions.append(
                        AgentAction(
                            type="tool_call",
                            payload=_redact_tool_result_for_model(blocked_result),
                        )
                    )
                    continue
                prepared = self.tools.prepare_or_execute(
                    call.name,
                    execution_arguments,
                    _tool_call_reason(call),
                    tool_call_id=call.id,
                )
                check_cancelled(cancel_checker)
                if isinstance(prepared, PendingToolAction):
                    prepared = prepared.with_continuation_messages(
                        _build_pending_continuation_messages(
                            working_messages,
                            turn.message,
                            tool_messages,
                            turn.tool_calls,
                            pending_call_id=call.id,
                        )
                    )
                    skipped_after_pending = _build_skipped_after_pending_messages(
                        turn.tool_calls,
                        start_after_call_id=call.id,
                    )
                    tool_messages.extend(skipped_after_pending)
                    debug_log(
                        "AgentRuntime",
                        "工具调用等待用户确认",
                        {
                            **prepared.to_dict(),
                            "continuation_message_count": len(prepared.continuation_messages),
                        },
                    )
                    pending_actions.append(prepared)
                    break

                if _is_screen_observation_request(prepared):
                    if allow_screen_observation:
                        screen_action = AgentAction(
                            type=SCREEN_OBSERVATION_REQUEST_ACTION,
                            payload={"reason": _tool_call_reason(call)},
                        )
                        debug_log(
                            "AgentRuntime",
                            "请求屏幕观察 follow-up",
                            {
                                "step_index": step_index,
                                "reason": _tool_call_reason(call),
                                "turn_elapsed_ms": int((time.perf_counter() - turn_started_at) * 1000),
                            },
                        )
                        return AgentResult(
                            reply=_build_screen_observation_request_reply(),
                            actions=[*emitted_actions, screen_action],
                        )
                    prepared = ToolExecutionResult(
                        tool_name=OBSERVE_SCREEN_TOOL_NAME,
                        success=False,
                        content="",
                        error=SCREEN_OBSERVATION_DISABLED_ERROR,
                    )

                debug_log("AgentRuntime", "工具调用完成", _redact_tool_result_for_model(prepared))
                tool_routing.remember_memory_tool_result(
                    tool_name=call.name,
                    arguments=execution_arguments,
                    result=prepared,
                    result_cache=memory_tool_result_cache,
                )
                step_results.append(prepared)
                execution_results.append(prepared)
                tool_messages.extend(
                    _build_tool_messages_for_result(
                        call,
                        prepared,
                        include_images=self.model_vision_enabled,
                    )
                )
                if call.name == "search_tools":
                    active_groups.update(_groups_from_search_tools_result(prepared))
                emitted_actions.append(
                    AgentAction(
                        type="tool_call",
                        payload=_redact_tool_result_for_model(prepared),
                    )
                )
                if (
                    call.name in {"web__fetch_url", "fetch_url"}
                    and prepared.success
                    and not (isinstance(prepared.content, dict) and prepared.content.get("skipped"))
                    and not (isinstance(prepared.content, dict) and prepared.content.get("auto_fetched"))
                ):
                    fetch_index = len(tool_routing._successful_web_fetches(execution_results))
                    ja, zh = tool_routing.build_web_fetch_progress_texts(
                        prepared,
                        index=max(1, fetch_index),
                    )
                    _emit_progress_reply(
                        progress_callback,
                        ja=ja,
                        zh=zh,
                        stage="web_fetch",
                        metadata={
                            "step_index": step_index,
                            "tool_names": [call.name],
                        },
                        cancel_checker=cancel_checker,
                    )

            skipped_calls = len(turn.tool_calls) - allowed_calls
            if skipped_calls > 0:
                debug_log(
                    "AgentRuntime",
                    "工具调用数量超过上限",
                    {
                        "step_index": step_index,
                        "requested": len(turn.tool_calls),
                        "allowed": allowed_calls,
                        "total_tool_calls": total_tool_calls,
                        "step_limit": loop_settings.max_tool_calls_per_step,
                        "turn_limit": loop_settings.max_tool_calls_per_turn,
                    },
                )
                for skipped_call in turn.tool_calls[allowed_calls:]:
                    limit_error = (
                        f"本步骤最多执行 {loop_settings.max_tool_calls_per_step} 个工具调用，"
                        f"整轮最多执行 {loop_settings.max_tool_calls_per_turn} 个工具调用，"
                        f"已跳过后续调用 {skipped_call.name}。"
                    )
                    limit_result = ToolExecutionResult(
                        tool_name="runtime",
                        success=False,
                        content={
                            "skipped": True,
                            "reason": "tool_call_limit",
                            "tool_name": skipped_call.name,
                        },
                        error=limit_error,
                    )
                    step_results.append(limit_result)
                    execution_results.append(limit_result)
                    tool_messages.extend(
                        _build_tool_messages_for_result(
                            skipped_call,
                            limit_result,
                            include_images=self.model_vision_enabled,
                        )
                    )
                    emitted_actions.append(
                        AgentAction(
                            type="tool_call",
                            payload=_redact_tool_result_for_model(limit_result),
                        )
                    )

            executed_calls = [
                _native_tool_call_to_policy_call(call, _tool_arguments_for_execution(call, self.tools))
                for call in turn.tool_calls[:allowed_calls]
            ]
            if tool_routing._should_auto_snapshot_after_browser_navigation(executed_calls, step_results, self.tools):
                check_cancelled(cancel_checker)
                snapshot_result = tool_routing._execute_auto_browser_snapshot(
                    self.tools,
                    step_index,
                )
                check_cancelled(cancel_checker)
                step_results.append(snapshot_result)
                execution_results.append(snapshot_result)
                # 独立 tool_call_id 而非复用 navigate 的 id（navigate 的结果已消费该 id，
                # 复用会被 sanitize 丢弃）。必须声明进 assistant 消息的 tool_calls，
                # 否则 sanitize_tool_conversation_messages 同样丢弃。
                auto_snapshot_id = f"auto_browser_snapshot_{step_index}"
                auto_tool_calls.append(
                    _auto_tool_call_entry(
                        auto_snapshot_id,
                        BROWSER_SNAPSHOT_TOOL_NAME,
                        "{}",
                    )
                )
                tool_messages.extend(
                    _build_tool_messages_for_result(
                        NativeToolCall(
                            id=auto_snapshot_id,
                            name=BROWSER_SNAPSHOT_TOOL_NAME,
                            arguments={},
                            arguments_json="{}",
                        ),
                        snapshot_result,
                        include_images=self.model_vision_enabled,
                    )
                )
                emitted_actions.append(
                    AgentAction(
                        type="tool_call",
                        payload=_redact_tool_result_for_model(snapshot_result),
                    )
                )
                should_fast_forward_final_reply = tool_routing._should_fast_forward_after_auto_browser_snapshot(
                    working_messages,
                    snapshot_result,
                )

            if tool_routing._should_refine_web_search(working_messages, execution_results):
                refined_query = tool_routing.build_refined_web_search_query(working_messages)
                if refined_query:
                    check_cancelled(cancel_checker)
                    try:
                        from app.agent.mcp import web_search_server as web_mod

                        refined_payload = web_mod.search_web(refined_query, max_results=5)
                        refined_result = ToolExecutionResult(
                            tool_name="web__web_search",
                            success=bool(refined_payload.get("results")),
                            content={**refined_payload, "refined_query": refined_query},
                            error="",
                        )
                    except Exception as exc:
                        refined_result = ToolExecutionResult(
                            tool_name="web__web_search",
                            success=False,
                            content={"query": refined_query, "refined_query": refined_query},
                            error=str(exc),
                        )
                    step_results.append(refined_result)
                    execution_results.append(refined_result)
                    # 独立 tool_call_id，并声明进 assistant 消息，避免 sanitize 丢弃
                    auto_refine_id = f"auto_web_search_refine_{step_index}"
                    auto_tool_calls.append(
                        _auto_tool_call_entry(
                            auto_refine_id,
                            "web__web_search",
                            json.dumps({"query": refined_query}, ensure_ascii=False),
                        )
                    )
                    refined_call = NativeToolCall(
                        id=auto_refine_id,
                        name="web__web_search",
                        arguments={"query": refined_query},
                        arguments_json="{}",
                    )
                    tool_messages.extend(
                        _build_tool_messages_for_result(
                            refined_call,
                            refined_result,
                            include_images=self.model_vision_enabled,
                        )
                    )
                    emitted_actions.append(
                        AgentAction(
                            type="tool_call",
                            payload=_redact_tool_result_for_model(refined_result),
                        )
                    )

            if tool_routing._should_auto_fetch_after_web_search(
                working_messages,
                step_results,
                execution_results,
            ):
                user_query = tool_routing._latest_user_text(working_messages) or ""
                auto_urls = tool_routing._select_urls_for_auto_fetch(
                    tool_routing._successful_web_searches(step_results),
                    max_urls=4,
                    query=user_query,
                )

                def _on_auto_fetch_page(fetch_index: int, fetch_result: ToolExecutionResult) -> None:
                    check_cancelled(cancel_checker)
                    step_results.append(fetch_result)
                    execution_results.append(fetch_result)
                    fetch_url = ""
                    reader = "fetch_url"
                    if isinstance(fetch_result.content, dict):
                        fetch_url = str(fetch_result.content.get("url") or "")
                        reader = str(fetch_result.content.get("reader") or reader)
                    tool_name = fetch_result.tool_name or "web__fetch_url"
                    auto_fetch_id = f"auto_web_fetch_{step_index}_{fetch_index}"
                    auto_tool_calls.append(
                        _auto_tool_call_entry(
                            auto_fetch_id,
                            tool_name,
                            json.dumps({"url": fetch_url}, ensure_ascii=False),
                        )
                    )
                    auto_call = NativeToolCall(
                        id=auto_fetch_id,
                        name=tool_name,
                        arguments={"url": fetch_url},
                        arguments_json="{}",
                    )
                    tool_messages.extend(
                        _build_tool_messages_for_result(
                            auto_call,
                            fetch_result,
                            include_images=self.model_vision_enabled,
                        )
                    )
                    emitted_actions.append(
                        AgentAction(
                            type="tool_call",
                            payload=_redact_tool_result_for_model(fetch_result),
                        )
                    )
                    ja, zh = tool_routing.build_web_fetch_progress_texts(
                        fetch_result,
                        index=fetch_index,
                    )
                    if ja or zh:
                        _emit_progress_reply(
                            progress_callback,
                            ja=ja,
                            zh=zh,
                            stage="web_fetch",
                            metadata={
                                "step_index": step_index,
                                "tool_names": [tool_name],
                                "auto_fetched": True,
                                "page_index": fetch_index,
                                "reader": reader,
                            },
                            cancel_checker=cancel_checker,
                        )

                auto_fetch_results = tool_routing._execute_auto_web_fetches(
                    auto_urls,
                    step_index=step_index,
                    max_keep=3,
                    enough_chars=1600,
                    on_page=_on_auto_fetch_page,
                    tools=self.tools,
                )
                # 全部失败时仍写入一条失败结果，便于日志与收束。
                if auto_fetch_results and not any(item.success for item in auto_fetch_results):
                    for fetch_index, fetch_result in enumerate(auto_fetch_results, start=1):
                        check_cancelled(cancel_checker)
                        step_results.append(fetch_result)
                        execution_results.append(fetch_result)
                        fetch_url = ""
                        if isinstance(fetch_result.content, dict):
                            fetch_url = str(fetch_result.content.get("url") or "")
                        tool_name = fetch_result.tool_name or "web__fetch_url"
                        auto_fetch_id = f"auto_web_fetch_{step_index}_{fetch_index}"
                        auto_tool_calls.append(
                            _auto_tool_call_entry(
                                auto_fetch_id,
                                tool_name,
                                json.dumps({"url": fetch_url}, ensure_ascii=False),
                            )
                        )
                        auto_call = NativeToolCall(
                            id=auto_fetch_id,
                            name=tool_name,
                            arguments={"url": fetch_url},
                            arguments_json="{}",
                        )
                        tool_messages.extend(
                            _build_tool_messages_for_result(
                                auto_call,
                                fetch_result,
                                include_images=self.model_vision_enabled,
                            )
                        )
                        emitted_actions.append(
                            AgentAction(
                                type="tool_call",
                                payload=_redact_tool_result_for_model(fetch_result),
                            )
                        )

            if not should_fast_forward_final_reply and tool_routing._should_fast_forward_after_web_search(
                working_messages,
                execution_results,
            ):
                should_fast_forward_final_reply = True
                debug_log(
                    "AgentRuntime",
                    "网页搜索已取得结果，进入最终总结",
                    {
                        "step_index": step_index,
                        "tool_result_count": len(execution_results),
                        "deep_lookup": tool_routing._latest_user_is_deep_web_lookup(working_messages),
                        "fetch_count": len(tool_routing._successful_web_fetches(execution_results)),
                    },
                )

            if not should_fast_forward_final_reply and tool_routing.should_fast_forward_after_memory_search(
                execution_results,
                recall_decision=turn_state.recall_decision,
            ):
                should_fast_forward_final_reply = True
                debug_log(
                    "AgentRuntime",
                    "记忆搜索已达预算，进入最终总结",
                    {
                        "step_index": step_index,
                        "tool_result_count": len(execution_results),
                        "recall_decision": turn_state.recall_decision,
                        "memory_search_count": tool_routing.count_successful_memory_searches(
                            execution_results
                        ),
                    },
                )

            if pending_actions:
                debug_log(
                    "AgentRuntime",
                    "返回待确认动作",
                    {
                        "step_index": step_index,
                        "pending_actions": [action.to_dict() for action in pending_actions],
                        "tools_elapsed_ms": int((time.perf_counter() - tools_started_at) * 1000),
                        "turn_elapsed_ms": int((time.perf_counter() - turn_started_at) * 1000),
                    },
                )
                return AgentResult(
                    reply=_build_pending_action_reply(pending_actions),
                    _debug=_build_debug_meta(
                        turn_client, execution_results,
                        total_tool_calls, turn_started_at,
                        self.get_last_prompt_inspection(),
                        turn_state=turn_state,
                    ),
                    actions=[
                        *emitted_actions,
                        *[
                            AgentAction(
                                type="pending_action",
                                payload=action.to_dict(include_context=True),
                            )
                            for action in pending_actions
                        ],
                    ],
                )

            if not step_results:
                break

            working_messages.append(
                _extend_assistant_with_tool_calls(turn.message, auto_tool_calls)
            )
            working_messages.extend(tool_messages)
            # 本步若写过记忆，下一步重新执行相关记忆召回。
            if any(
                getattr(result, "tool_name", "") in {"memory_remember", "memory_forget"}
                for result in step_results
            ):
                memory_needs_refresh = True
            # 规划轮已带合格完整回复，且工具不会改写答案 → 不必再开下一轮规划
            if self._tool_loop_reusable_reply(
                turn, execution_results=execution_results
            ) is not None:
                debug_log(
                    "AgentRuntime",
                    "工具轮已含完整回复，结束工具循环",
                    {
                        "step_index": step_index,
                        "content_chars": len((turn.content or "").strip()),
                        "tool_result_count": len(execution_results),
                        "turn_elapsed_ms": int((time.perf_counter() - turn_started_at) * 1000),
                    },
                )
                break
            if should_fast_forward_final_reply:
                # 搜/读已完成：把确定性证据包放进上下文，避免终局合成仍处在「还在查」的中间态。
                evidence_packet = _build_web_search_evidence_packet_message(execution_results)
                if evidence_packet is not None:
                    working_messages.append(evidence_packet)
                debug_log(
                    "AgentRuntime",
                    "工具结果已足够，进入最终总结",
                    {
                        "step_index": step_index,
                        "tool_result_count": len(execution_results),
                        "web_evidence_packet": evidence_packet is not None,
                        "turn_elapsed_ms": int((time.perf_counter() - turn_started_at) * 1000),
                    },
                )
                break
            if total_tool_calls >= loop_settings.max_tool_calls_per_turn:
                break

        try:
            check_cancelled(cancel_checker)
            final_started_at = time.perf_counter()
            reusable = self._tool_loop_reusable_reply(
                last_turn,
                execution_results=execution_results,
            )
            if reusable is not None:
                # 工具轮已带合格完整回复：跳过二次 pro 合成（省一轮时间+token）
                snapshot = self._build_single_context_snapshot(
                    working_messages,
                    source=context_source,
                    memory_fragments=turn_memory_fragments if not memory_needs_refresh else None,
                    memory_status=memory_status if not memory_needs_refresh else None,
                )
                prompt_build = self._build_final_reply_result(snapshot)
                self._record_prompt_inspection(prompt_build.inspection)
                debug_log(
                    "AgentRuntime",
                    "工具轮已含完整回复，跳过最终合成",
                    {
                        "content_chars": len(reusable),
                        "had_tool_calls": bool(last_turn and last_turn.tool_calls),
                        "tool_result_count": len(execution_results),
                    },
                )
                parsed = self._parse_final_reply_with_retry(
                    prompt_build.system_prompt,
                    working_messages,
                    reusable,
                    runtime_context=prompt_build.runtime_context,
                    cancel_checker=cancel_checker,
                    turn_state=turn_state,
                )
                final_reply = self._seal_reply_tones(parsed.reply)
            else:
                final_reply = self._finalize_tool_loop_reply(
                    working_messages,
                    context_source=context_source,
                    cancel_checker=cancel_checker,
                    turn_state=turn_state,
                    memory_fragments=turn_memory_fragments,
                    memory_status=memory_status,
                    reuse_memory_fragments=not memory_needs_refresh,
                    execution_results=execution_results,
                )
            check_cancelled(cancel_checker)
        except OperationCancelled:
            raise
        except Exception as exc:
            debug_log("AgentRuntime", "工具结果总结失败，使用本地兜底回复", {"error": str(exc)})
            final_reply = _build_fallback_tool_reply(execution_results)
        debug_log(
            "AgentRuntime",
            "最终回复生成完成",
            {
                "segments": len(final_reply.segments),
                "actions": [_redact_tool_result_for_model(result) for result in execution_results],
                "final_reply_elapsed_ms": int((time.perf_counter() - final_started_at) * 1000),
                "turn_elapsed_ms": int((time.perf_counter() - turn_started_at) * 1000),
            },
        )
        return AgentResult(
            reply=self._normalize_reply(final_reply),
            actions=emitted_actions,
        )


    def handle_confirmed_action(
        self,
        action: PendingToolAction,
        progress_callback: ProgressCallback | None = None,
        cancel_checker: CancelChecker | None = None,
    ) -> AgentResult:
        check_cancelled(cancel_checker)
        turn_started_at = time.perf_counter()
        debug_log("AgentRuntime", "执行已确认动作", action.to_dict())
        result = self.tools.execute(action.tool_name, action.arguments)
        check_cancelled(cancel_checker)
        results = [result]
        verification_result = _verify_confirmed_windows_click(self.tools, action.tool_name)
        if verification_result is not None:
            results.append(verification_result)
        emitted_actions = [
            AgentAction(
                type="tool_call",
                payload=_redact_tool_result_for_model(item),
            )
            for item in results
        ]
        if action.continuation_messages:
            if action.tool_call_id:
                confirmed_messages = [
                    _build_tool_role_message(
                        NativeToolCall(
                            id=action.tool_call_id,
                            name=action.tool_name,
                            arguments=action.arguments,
                            arguments_json=json.dumps(action.arguments, ensure_ascii=False),
                        ),
                        result,
                    )
                ]
                if self.model_vision_enabled:
                    image_message = _build_tool_result_image_message([result])
                    if image_message is not None:
                        confirmed_messages.append(image_message)
                if len(results) > 1:
                    confirmed_messages.append(_build_confirmed_action_result_message(action, results[1:]))
            else:
                confirmed_messages = [_build_confirmed_action_result_message(action, results)]
            working_messages = [
                *action.continuation_messages,
                *confirmed_messages,
            ]
            allow_screen_observation = (
                self.model_vision_enabled
                and self.autonomous_screen_observation_enabled
                and not messages_contain_image(working_messages)
                and tool_routing._should_offer_screen_observation(working_messages)
            )
            debug_log(
                "AgentRuntime",
                "已确认动作接回 Agent 循环",
                {
                    "tool_name": action.tool_name,
                    "message_count": len(working_messages),
                    "allow_screen_observation": allow_screen_observation,
                },
            )
            return self._run_tool_loop(
                working_messages,
                allow_screen_observation=allow_screen_observation,
                turn_started_at=turn_started_at,
                context_source="confirmed_action",
                planning_extra_instructions=_build_confirmed_action_continuation_rules(action),
                initial_actions=emitted_actions,
                progress_callback=progress_callback,
                cancel_checker=cancel_checker,
            )
        final_messages = [_build_confirmed_action_result_message(action, results)]
        snapshot = self._build_single_context_snapshot(
            final_messages, source="confirmed_action"
        )
        prompt_build = self._build_final_reply_result(snapshot)
        self._record_prompt_inspection(prompt_build.inspection)
        try:
            check_cancelled(cancel_checker)
            reply = self._client_for_messages(final_messages).chat(
                prompt_build.system_prompt,
                final_messages,
                self._effective_reply_tones(),
                self.reply_portraits,
                runtime_context=prompt_build.runtime_context,
                cancel_checker=cancel_checker,
                on_chunk=_build_stream_progress_emitter(progress_callback, cancel_checker),
                verbosity_guidance=self._turn_verbosity_guidance or None,
            )
            self._record_runtime_role(prompt_build.inspection)
            check_cancelled(cancel_checker)
        except OperationCancelled:
            raise
        except Exception as exc:
            debug_log("AgentRuntime", "确认动作总结失败，使用本地兜底回复", {"error": str(exc)})
            reply = _build_fallback_tool_reply(results)
        debug_log(
            "AgentRuntime",
            "已确认动作处理完成",
            {
                "results": [_redact_tool_result_for_model(item) for item in results],
                "segments": len(reply.segments),
            },
        )
        return AgentResult(
            reply=self._normalize_reply(reply),
            actions=emitted_actions,
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
                                "portrait": "站立待机",
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


    def handle_event(
        self,
        event: AgentEvent,
        progress_callback: ProgressCallback | None = None,
        cancel_checker: CancelChecker | None = None,
    ) -> AgentResult:
        check_cancelled(cancel_checker)
        if event.type not in {"reminder_due", "screen_awareness_check", "proactive_check", "user_interaction"}:
            return AgentResult(reply=parse_chat_reply("未対応のイベントだよ。"))

        debug_log("AgentRuntime", "处理主动事件", {"event": {"type": event.type, "payload": event.payload}})
        event_messages = _build_event_messages(event)
        event_action = AgentAction(
            type="event",
            payload={
                "event_type": event.type,
                "event_payload": event.payload,
            },
        )
        # screen_awareness_check / proactive_check：旧定时批次主动事件。
        # PetWindow 已不再调度这两类；保留分支仅兼容测试/残留调用。主动看屏见 ProactiveObserver。
        if event.type in {"screen_awareness_check", "proactive_check"}:
            screen_context_allowed = bool(event.payload.get("screen_context_allowed"))
            allow_screen_observation = (
                screen_context_allowed
                and not messages_contain_image(event_messages)
            )
            return self._run_tool_loop(
                event_messages,
                allow_screen_observation=allow_screen_observation,
                turn_started_at=time.perf_counter(),
                proactive_mode=True,
                context_source="event",
                event_type=event.type,
                event_payload=event.payload,
                initial_actions=[event_action],
                vision_unsupported_reply=_build_proactive_vision_unsupported_reply(),
                progress_callback=progress_callback,
                cancel_checker=cancel_checker,
            )

        snapshot = self._build_single_context_snapshot(
            event_messages,
            source="event",
            mode="proactive" if event.type in {"screen_awareness_check", "proactive_check"} else "normal",
            event_type=event.type,
            event_payload=event.payload,
        )
        prompt_build = self._build_event_reply_result(event.type, snapshot)
        self._record_prompt_inspection(prompt_build.inspection)
        try:
            check_cancelled(cancel_checker)
            reply = self._request_structured_event_reply(
                prompt_build,
                event_messages,
                cancel_checker=cancel_checker,
                progress_callback=progress_callback,
            )
            self._record_runtime_role(prompt_build.inspection)
            check_cancelled(cancel_checker)
        except ApiRequestError as exc:
            if messages_contain_image(event_messages) and is_vision_unsupported_error(exc):
                debug_log("AgentRuntime", "主动事件视觉输入不受支持，返回兜底回复", {"error": str(exc)})
                return AgentResult(reply=_build_proactive_vision_unsupported_reply())
            raise
        return AgentResult(
            reply=reply,
            actions=[event_action],
        )


    def _request_structured_event_reply(
        self,
        prompt_build,
        event_messages: list[ChatMessage],
        *,
        cancel_checker: CancelChecker | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> ChatReply:
        """事件回复走结构化 JSON + 格式修复，避免 api_client.chat 直接降级兜底。

        event.rules 里已含分段协议；这里只补一句 JSON 约束，不再整段重贴
        reply.protocol / portrait_hints（避免事件路径多浪费约 2K tokens）。
        """
        # 协议已在 _build_event_reply_result 的 event.rules 中；勿再 append 全量立绘提示。
        json_only_reminder = (
            "最终回复必须是合法 JSON segments 对象，禁止纯文本、Markdown 或代码块。"
        )
        full_system_prompt = f"{prompt_build.system_prompt.strip()}\n\n{json_only_reminder}"
        dialogue_temperature, dialogue_extra_params = self._resolve_dialogue_params()
        event_temperature = min(dialogue_temperature, 0.5)
        on_chunk = _build_stream_progress_emitter(progress_callback, cancel_checker)

        if progress_callback is not None:
            chunks: list[str] = []
            for chunk in self._client_for_messages(event_messages).stream_raw(
                full_system_prompt,
                event_messages,
                temperature=event_temperature,
                response_format=STRUCTURED_JSON_RESPONSE_FORMAT,
                cancel_checker=cancel_checker,
                runtime_context=prompt_build.runtime_context,
                **dialogue_extra_params,
            ):
                chunks.append(chunk)
                on_chunk(chunk)
            raw_content = "".join(chunks)
        else:
            raw_content = self._client_for_messages(event_messages).complete_raw(
                full_system_prompt,
                event_messages,
                temperature=event_temperature,
                response_format=STRUCTURED_JSON_RESPONSE_FORMAT,
                cancel_checker=cancel_checker,
                runtime_context=prompt_build.runtime_context,
                **dialogue_extra_params,
            )

        parsed = self._parse_final_reply_with_retry(
            prompt_build.system_prompt,
            event_messages,
            raw_content,
            runtime_context=prompt_build.runtime_context,
            cancel_checker=cancel_checker,
        )
        return self._normalize_reply(self._seal_reply_tones(parsed.reply))


