from __future__ import annotations

from pathlib import Path

import json
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from dataclasses import dataclass, replace
from threading import Lock
from typing import Any, Callable, cast

from app.agent.actions import AgentAction, AgentEvent, AgentProgress, AgentResult, PendingToolAction
from app.agent.context_orchestrator import ContextOrchestrator, build_context_request
from app.agent.memory_recall import MemoryRecallService
from app.agent.memory import MemoryStore
from app.agent.screen_awareness import SCREEN_AWARENESS_IMAGE_DETAIL
from app.agent.screen_tools import (
    OBSERVE_SCREEN_TOOL_NAME,
    SCREEN_OBSERVATION_CAPABILITY,
    SCREEN_OBSERVATION_DISABLED_ERROR,
    SCREEN_OBSERVATION_REQUEST_ACTION,
)
from app.agent.screen_policy import ScreenPolicy
from app.agent.session_state_context import (
    SESSION_DIGEST_INJECT_MAX_RECENT_MESSAGES,
    build_session_state_fragment,
)
from app.agent.sensory_context import build_sensory_impression_fragment
from app.agent.lore import LoreIndex, build_lore_context_fragment, load_lore_index
from app.agent.local_context import build_media_context_fragment
from app.agent.reply_verbosity import (
    decision_from_interest,
    format_verbosity_guidance,
)
from app.agent.inner_thought import (
    InnerThoughtResult,
    InnerThoughtSettings,
    InnerThoughtWindow,
    build_inner_thought_fragment,
    format_recent_dialogue,
    generate_inner_thought,
    load_character_excerpt,
    mood_summary_from_store,
    sensory_impression_text,
    should_generate_inner_thought,
)
from app.agent.tool_policy import (
    BROWSER_NAVIGATE_TOOL_NAME,
    BROWSER_SNAPSHOT_TOOL_NAME,
    ToolPolicy,
    WINDOWS_CLICK_TOOL_NAME,
    WINDOWS_SCREENSHOT_TOOL_NAME,
    WINDOWS_SNAPSHOT_TOOL_NAME,
)
import app.agent.tool_routing as tool_routing
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
from app.agent.tools import ToolExecutionResult, ToolRegistry
from app.storage.chat_history import ChatHistoryStore
from app.llm.api_client import (
    ApiRequestError,
    ChatCompletionTurn,
    ChatMessage,
    NativeToolCall,
    OpenAICompatibleClient,
    STRUCTURED_JSON_RESPONSE_FORMAT,
    is_vision_unsupported_error,
    messages_contain_image,
    strip_image_parts_from_messages,
)
from app.llm.context_trimming import trim_messages_for_model
from app.llm.chat_reply import ChatReply, ChatReplyParseResult, ChatSegment, DEFAULT_TONE, parse_chat_reply, parse_chat_reply_result, sanitize_reply_tones
from app.config.character_loader import CharacterProfile, normalize_reply_portraits
from app.core.cancellation import CancelChecker, OperationCancelled, check_cancelled
from app.core.debug_log import debug_body_enabled, debug_log, summarize_messages
from app.agent.runtime_limits import (
    MAX_EVENT_RECENT_CONVERSATION_CONTENT_CHARS,
    MAX_EVENT_RECENT_CONVERSATION_MESSAGES,
    MAX_PENDING_CONTEXT_MESSAGES,
    MAX_PENDING_CONTEXT_TEXT_CHARS,
    MAX_TOOL_RESULT_CHARS,
    ProgressCallback,
    RuntimeLoopSettings,
    normalize_runtime_loop_settings,
)
from app.llm.prompt_templates import (
    build_agent_reply_protocol,
    build_context_acquisition_strategy,
    build_event_system_prompt,
    build_proactive_check_tool_system_prefix,
)
from app.llm.prompts.recipes import build_segmented_reply_instruction
from app.plugins.models import ContextProviderContribution, PromptPatchContribution

from app.llm.prompts.runtime import PromptRuntime
from app.llm.prompts.types import (
    ContextFragment,
    ContextRequest,
    ContextSnapshot,
    PromptInspection,
    PromptRecipe,
    PromptSection,
)

_INTIMACY_GUIDE_PATH = Path(__file__).resolve().parents[2] / "data" / "intimacy_guide.txt"
# 亲密节奏开启时追加；分流细则见 gitignore 的 intimacy_guide.txt
_INTIMACY_EXTRA_TONES: tuple[str, ...] = ("亲密", "H")
_INTIMACY_ENTRY_HINT = (
    "# 节奏工具\n"
    "若本轮双方已同意、正在准备或即将开始身体亲密"
    "（答应一起做、开始靠近/触碰、动手前的准备，或你准备用 tone「亲密」/「H」），"
    "必须先调用 set_intimacy_mode(on=true)，再写回复。"
    "不要等到做到一半才开；准备阶段就要开。"
    "普通暧昧试探、口头调情、尚未准备动手时不要开。"
    "开启后才能使用 tone「亲密」与「H」，并获得身体亲密向的演出引导。"
)


def _load_intimacy_guide() -> str:
    try:
        return _INTIMACY_GUIDE_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


_STRUCTURED_COMPOSE_RETRY_REASONS = frozenset({
    "missing_translation",
    "missing_segments",
    "invalid_json",
    "empty",
})


@dataclass
class _InnerThoughtLaunch:
    """step0 与记忆召回并行的内心独白任务句柄（仅 runtime 内部使用）。"""

    future: Future[str]
    executor: ThreadPoolExecutor


class AgentRuntime:
    """封装聊天决策链路，为后续工具调用和长期记忆留下扩展点。"""

    def __init__(
        self,
        api_client: OpenAICompatibleClient,
        system_prompt: str,
        reply_tones: list[str] | None = None,
        reply_portraits: list[str] | None = None,
        tools: ToolRegistry | None = None,
        memory: MemoryStore | None = None,
        history_store: ChatHistoryStore | None = None,
        prompt_patches: list[PromptPatchContribution] | None = None,
        context_providers: list[ContextProviderContribution] | None = None,
        runtime_loop_settings: RuntimeLoopSettings | None = None,
        vision_api_client: OpenAICompatibleClient | None = None,
        chat_fast_api_client: OpenAICompatibleClient | None = None,
        inner_thought_api_client: OpenAICompatibleClient | None = None,
        turn_routing_settings: TurnRoutingSettings | None = None,
        inner_thought_settings: InnerThoughtSettings | None = None,
        character_id: str = "",
        character_name: str = "",
    ) -> None:
        self.api_client = api_client
        self._vision_api_client = vision_api_client
        self._chat_fast_api_client = chat_fast_api_client
        self._inner_thought_api_client = inner_thought_api_client
        self.turn_routing_settings = turn_routing_settings or TurnRoutingSettings()
        self.inner_thought_settings = (
            inner_thought_settings or InnerThoughtSettings()
        ).normalized()
        self.system_prompt = system_prompt
        self.character_id = character_id.strip()
        self.character_name = character_name.strip()
        self.reply_tones = [*reply_tones] if reply_tones is not None else []
        self.reply_portraits = [*reply_portraits] if reply_portraits is not None else []
        self.character_profile: CharacterProfile | None = None
        self._lore_index: LoreIndex | None = None
        self._lore_index_path: str = ""
        self._turn_verbosity_guidance: str = ""
        self._turn_interest: str | None = None
        self.tools = tools or ToolRegistry()
        self.memory = memory or MemoryStore()
        self.history_store = history_store
        self.prompt_patches = [*prompt_patches] if prompt_patches is not None else []
        self.context_providers = (
            [*context_providers] if context_providers is not None else []
        )
        self.runtime_loop_settings = normalize_runtime_loop_settings(runtime_loop_settings)
        self.prompt_runtime = PromptRuntime()
        self.context_orchestrator = ContextOrchestrator()
        self.memory_recall = MemoryRecallService(
            self.memory,
            query_rewriter_client=self._chat_fast_api_client,
        )
        self._inner_thought_window = InnerThoughtWindow(
            self.inner_thought_settings.window_size
        )
        self._inner_thought_done_for_turn = False
        self._last_prompt_inspection: PromptInspection | None = None
        self._prompt_inspection_lock = Lock()
        self.model_vision_enabled = True
        self.autonomous_screen_observation_enabled = True
        # 渐进记忆检索：在召回结果后追加标题索引 + 工具提示
        # TODO: 目前仅 set_progressive_memory_enabled() 手动启用，未接入配置/UI 开关
        self._progressive_memory = False
        self._intimacy_guide = _load_intimacy_guide()

    @property
    def vision_api_client(self) -> OpenAICompatibleClient | None:
        return self._vision_api_client

    @vision_api_client.setter
    def vision_api_client(self, client: OpenAICompatibleClient | None) -> None:
        self._vision_api_client = client

    @property
    def chat_fast_api_client(self) -> OpenAICompatibleClient | None:
        return self._chat_fast_api_client

    @chat_fast_api_client.setter
    def chat_fast_api_client(self, client: OpenAICompatibleClient | None) -> None:
        self._chat_fast_api_client = client
        # 自动召回 query 改写共用 chat_fast（未配置时走启发式）。
        if hasattr(self, "memory_recall"):
            self.memory_recall.set_query_rewriter_client(client)

    @property
    def inner_thought_api_client(self) -> OpenAICompatibleClient | None:
        return self._inner_thought_api_client

    @inner_thought_api_client.setter
    def inner_thought_api_client(self, client: OpenAICompatibleClient | None) -> None:
        self._inner_thought_api_client = client

    def _client_for_messages(self, messages: list[ChatMessage]) -> OpenAICompatibleClient:
        """含图消息优先走独立视觉 client；未配置时回退主 client。"""
        if messages_contain_image(messages) and self._vision_api_client is not None:
            return self._vision_api_client
        return self.api_client

    def _client_for_turn(
        self,
        messages: list[ChatMessage],
        turn_plan: TurnPlan,
    ) -> OpenAICompatibleClient:
        """按 TurnPlan 选择 LLM 客户端；含图时仍优先视觉 client。"""
        if messages_contain_image(messages) and self._vision_api_client is not None:
            return self._vision_api_client
        if turn_plan.client_key == "chat_fast" and self._chat_fast_api_client is not None:
            return self._chat_fast_api_client
        return self.api_client

    def update_character(
        self,
        system_prompt: str,
        reply_tones: list[str] | None = None,
        reply_portraits: list[str] | None = None,
        *,
        character_profile: CharacterProfile | None = None,
    ) -> None:
        """角色切换后同步系统提示词、可用语气、可用立绘与情绪映射。"""
        self.system_prompt = system_prompt
        self.reply_tones = [*reply_tones] if reply_tones is not None else []
        self.reply_portraits = [*reply_portraits] if reply_portraits is not None else []
        self.character_profile = character_profile
        # 换角色后清空内心独白窗口，避免串戏
        self._inner_thought_window.clear()
        self._reload_lore_index()
        if character_profile is not None:
            self.character_id = character_profile.id.strip()
            self.character_name = character_profile.display_name.strip()

    def _reload_lore_index(self) -> None:
        profile = self.character_profile
        path = profile.lore_index_path if profile is not None else None
        path_key = str(path) if path is not None else ""
        if path_key == self._lore_index_path:
            return
        self._lore_index_path = path_key
        self._lore_index = load_lore_index(path) if path is not None else None

    def set_prompt_patches(self, prompt_patches: list[PromptPatchContribution] | None) -> None:
        """同步插件提示词补丁。"""
        self.prompt_patches = [*prompt_patches] if prompt_patches is not None else []

    def set_context_providers(
        self,
        context_providers: list[ContextProviderContribution] | None,
    ) -> None:
        """同步插件动态上下文提供者。"""
        self.context_providers = (
            [*context_providers] if context_providers is not None else []
        )

    def set_history_store(
        self,
        history_store: ChatHistoryStore | None,
    ) -> None:
        """同步当前角色的聊天历史存储（跨会话续接的数据来源）。"""
        self.history_store = history_store

    def _enrich_event_payload(
        self,
        event_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """补全 seconds_since_pet_interaction：优先沿用事件侧已有值，否则从历史算对话间隙。"""
        payload = dict(event_payload or {})
        existing = payload.get("seconds_since_pet_interaction")
        if isinstance(existing, (int, float)):
            return payload
        gap = self._seconds_since_previous_user_message()
        if gap is not None:
            payload["seconds_since_pet_interaction"] = gap
        return payload

    def _seconds_since_previous_user_message(self) -> int | None:
        """当前用户消息写入后，距上一条历史（任意角色）的秒数。

        用「上一条对话活动 → 现在」比「上上条 user → 当前 user」更贴近停顿感：
        她刚说完你过很久才回，也会算进间隔。
        """
        from app.agent.time_awareness import parse_iso_datetime

        store = self.history_store
        if store is None:
            return None
        try:
            entries, _has_more = store.load_tail(40)
        except Exception:  # noqa: BLE001
            return None
        if len(entries) < 2:
            return None
        # load_tail 按时间升序；末条通常是刚写入的当前 user
        previous = entries[-2]
        then = parse_iso_datetime(str(previous.created_at or ""))
        if then is None:
            return None
        delta = (datetime.now().astimezone() - then).total_seconds()
        if delta < 0:
            return None
        return int(delta)

    def _launch_inner_thought_worker(
        self,
        working_messages: list[ChatMessage],
        turn_state: TurnState,
        *,
        proactive_mode: bool,
    ) -> _InnerThoughtLaunch | None:
        """主线程拍快照后提交 Flash；与记忆召回并行。跳过则返回 None。"""
        if self._inner_thought_done_for_turn:
            return None
        settings = self.inner_thought_settings.normalized()
        self._inner_thought_window.configure(settings.window_size)
        if not should_generate_inner_thought(
            settings,
            api_client=self._inner_thought_api_client,
            turn_tier=turn_state.turn_plan.tier,
            proactive_mode=proactive_mode,
        ):
            self._inner_thought_done_for_turn = True
            return None
        assert self._inner_thought_api_client is not None
        self._inner_thought_done_for_turn = True
        profile = self.character_profile
        card_path = profile.card_path if profile is not None else None
        # 只读快照在主线程取齐，避免 worker 与召回争用可变会话状态
        character_name = self.character_name
        character_excerpt = load_character_excerpt(
            card_path=card_path,
            system_prompt=self.system_prompt,
        )
        mood_summary = mood_summary_from_store(self.memory)
        recent_dialogue = format_recent_dialogue(working_messages)
        sensory = sensory_impression_text()
        previous = self._inner_thought_window.items()
        api_client = self._inner_thought_api_client
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="inner-thought")
        future = executor.submit(
            generate_inner_thought,
            api_client,
            character_name=character_name,
            character_excerpt=character_excerpt,
            mood_summary=mood_summary,
            recent_dialogue=recent_dialogue,
            sensory_impression=sensory,
            previous_thoughts=previous,
            settings=settings,
        )
        debug_log(
            "InnerThought",
            "内心独白已与记忆召回并行启动",
            {"window": len(previous)},
        )
        return _InnerThoughtLaunch(future=future, executor=executor)

    def _finalize_inner_thought_worker(
        self,
        launch: _InnerThoughtLaunch | None,
    ) -> None:
        """join Flash worker；仅在主线程写入滑动窗口，并接通 interest→篇幅。"""
        if launch is None:
            return
        result = InnerThoughtResult(text="", interest=None)
        try:
            raw = launch.future.result()
            if isinstance(raw, InnerThoughtResult):
                result = raw
            elif isinstance(raw, str):
                # 兼容旧 mock / 仅返回正文的调用
                result = InnerThoughtResult(text=str(raw or "").strip(), interest=None)
            elif raw is not None:
                result = InnerThoughtResult(text=str(raw).strip(), interest=None)
        except Exception as exc:  # noqa: BLE001 — 独白失败不阻断主链路
            debug_log(
                "InnerThought",
                "内心独白并行任务异常，已跳过",
                {"error": str(exc)},
            )
            result = InnerThoughtResult(text="", interest=None)
        finally:
            launch.executor.shutdown(wait=True, cancel_futures=False)
        if result.text:
            self._inner_thought_window.push(result.text)
            debug_log(
                "InnerThought",
                "本轮内心独白已更新",
                {
                    "chars": len(result.text),
                    "interest": result.interest,
                    "window": len(self._inner_thought_window),
                },
            )
        self._apply_turn_interest(result.interest)

    def _session_state_fragments(
        self,
        request: ContextRequest,
    ) -> tuple[ContextFragment, ...]:
        fragments: list[ContextFragment] = []
        sensory = build_sensory_impression_fragment()
        if sensory is not None:
            fragments.append(sensory)
        thought = build_inner_thought_fragment(
            self._inner_thought_window,
            character_name=self.character_name,
        )
        if thought is not None:
            fragments.append(thought)

        current_input = str(getattr(request, "current_input", "") or "").strip()
        media_fragment = build_media_context_fragment(current_input)
        if media_fragment is not None:
            fragments.append(media_fragment)

        if self._lore_index is None and self.character_profile is not None:
            self._reload_lore_index()
        if self._lore_index is not None and current_input:
            history_payload: list[dict[str, str]] = []
            for message in request.recent_messages[-8:]:
                role = str(getattr(message, "role", "") or "").strip()
                content = str(getattr(message, "content", "") or "").strip()
                if role in {"user", "assistant"} and content:
                    history_payload.append({"role": role, "content": content})
            lore_fragment = build_lore_context_fragment(
                current_input,
                self._lore_index,
                history=history_payload,
            )
            if lore_fragment is not None:
                fragments.append(lore_fragment)

        store = self.history_store
        if store is None:
            return tuple(fragments)
        # 仅在会话刚开始（实时窗口尚浅）时才回看历史，避免每轮全量读盘与重复注入。
        if len(request.recent_messages) >= SESSION_DIGEST_INJECT_MAX_RECENT_MESSAGES:
            return tuple(fragments)
        try:
            entries = store.load()
            fragment = build_session_state_fragment(
                entries,
                recent_message_count=len(request.recent_messages),
                freshness=entries[-1].created_at if entries else "",
                current_input=request.current_input,
            )
        except Exception as exc:  # noqa: BLE001
            debug_log("SessionState", "最近会话状态读取失败，已跳过", {"error": str(exc)})
            return tuple(fragments)
        if fragment is not None:
            fragments.append(fragment)
        return tuple(fragments)

    def get_last_prompt_inspection(self) -> dict[str, Any] | None:
        """返回最近一次 Prompt 构建的脱敏检查结果。"""

        lock = getattr(self, "_prompt_inspection_lock", None)
        if lock is None:
            inspection = getattr(self, "_last_prompt_inspection", None)
        else:
            with lock:
                inspection = self._last_prompt_inspection
        if inspection is None:
            return None
        return inspection.to_dict(include_content=debug_body_enabled())

    def _record_prompt_inspection(self, inspection: PromptInspection) -> None:
        lock = getattr(self, "_prompt_inspection_lock", None)
        if lock is None:
            self._last_prompt_inspection = inspection
        else:
            with lock:
                self._last_prompt_inspection = inspection
        debug_log(
            "PromptInspector",
            "Prompt 构建完成",
            inspection.to_dict(include_content=debug_body_enabled()),
        )

    def _build_single_context_snapshot(
        self,
        messages: list[ChatMessage],
        *,
        source: str,
        mode: str = "normal",
        event_type: str = "",
        event_payload: dict[str, Any] | None = None,
        memory_fragments: tuple[Any, ...] | list[Any] | None = None,
        memory_status: str | None = None,
    ) -> ContextSnapshot:
        request = build_context_request(
            messages,
            source=source,
            mode=mode,
            event_type=event_type,
            step_index=0,
            remaining_steps=0,
            available_tools=(),
            event_payload=self._enrich_event_payload(event_payload),
            service_status={"memory": memory_status or "unknown"},
        )
        if memory_fragments is None:
            recall = self.memory_recall.recall(request)
            request = replace(request, service_status={"memory": recall.status})
            fragments = recall.fragments
        else:
            fragments = tuple(memory_fragments)
            if memory_status:
                request = replace(request, service_status={"memory": memory_status})
        return self.context_orchestrator.build_snapshot(
            request,
            providers=self.context_providers,
            session_fragments=self._session_state_fragments(request),
            memory_fragments=fragments,
        )

    def _record_runtime_role(self, inspection: PromptInspection) -> None:
        role = str(getattr(self.api_client, "runtime_context_role", inspection.runtime_role))
        if role != inspection.runtime_role:
            inspection = replace(inspection, runtime_role=role)
        self._record_prompt_inspection(inspection)


    def set_model_vision_enabled(self, enabled: bool) -> None:
        """允许模型在需要时请求一次当前屏幕截图。"""
        self.model_vision_enabled = enabled

    def set_autonomous_screen_observation_enabled(self, enabled: bool) -> None:
        """允许模型在对话或主动事件中自主决定是否观察屏幕。"""
        self.autonomous_screen_observation_enabled = enabled

    def set_progressive_memory_enabled(self, enabled: bool) -> None:
        """启用渐进记忆检索：在召回结果后追加标题索引 + 工具提示。"""
        self._progressive_memory = enabled

    def set_runtime_loop_settings(self, settings: RuntimeLoopSettings | None) -> None:
        """同步工具循环限制，后续对话从新设置开始生效。"""
        self.runtime_loop_settings = normalize_runtime_loop_settings(settings)

    def _resolve_dialogue_params(self) -> tuple[float, dict[str, Any]]:
        """读取角色对话生成参数，兼容测试桩和外部传入的旧客户端实现。"""
        return self._resolve_dialogue_params_for_client(self.api_client)

    def _resolve_dialogue_params_for_client(
        self,
        client: OpenAICompatibleClient,
    ) -> tuple[float, dict[str, Any]]:
        resolver = getattr(client, "resolve_dialogue_params", None)
        if callable(resolver):
            return resolver()
        return 0.8, {}

    def _resolve_dialogue_params_for_turn(
        self,
        turn_plan: TurnPlan,
        client: OpenAICompatibleClient,
    ) -> tuple[float, dict[str, Any]]:
        temperature, extra = self._resolve_dialogue_params_for_client(client)
        if not turn_plan.generation_params:
            return temperature, extra
        return temperature, {**extra, **turn_plan.generation_params}

    def _resolve_turn_state(
        self,
        working_messages: list[ChatMessage],
        request: ContextRequest,
        *,
        proactive_mode: bool,
    ) -> TurnState:
        settings = self.turn_routing_settings
        chat_fast_configured = self._chat_fast_api_client is not None
        recall_decision = resolve_recall_decision(
            working_messages,
            request,
            proactive_mode=proactive_mode,
            settings=settings,
        )
        classifier_result = None
        if should_invoke_turn_classifier(
            working_messages,
            proactive_mode=proactive_mode,
            chat_fast_configured=chat_fast_configured,
            settings=settings,
            recall_decision=recall_decision,
        ):
            classifier_client = self._chat_fast_api_client or self.api_client
            if isinstance(classifier_client, OpenAICompatibleClient):
                user_text = tool_routing._latest_user_text(working_messages) or request.current_input
                classifier_result = classify_turn_depth(
                    user_text,
                    client=classifier_client,
                    timeout_seconds=settings.classifier_timeout_seconds,
                )
        turn_plan = resolve_turn_plan(
            working_messages,
            request,
            proactive_mode=proactive_mode,
            has_vision_client=self._vision_api_client is not None,
            chat_fast_configured=chat_fast_configured,
            settings=settings,
            classifier_result=classifier_result,
            recall_decision=recall_decision,
        )
        debug_log(
            "AgentRuntime",
            "Turn 路由决策",
            {
                "recall_decision": recall_decision,
                "tier": turn_plan.tier,
                "modality": turn_plan.modality,
                "client_key": turn_plan.client_key,
                "decided_by": turn_plan.decided_by,
            },
        )
        return TurnState(turn_plan=turn_plan, recall_decision=recall_decision)

    def _compose_structured_final_reply(
        self,
        system_prompt: str,
        working_messages: list[ChatMessage],
        *,
        runtime_context: str,
        cancel_checker: CancelChecker | None = None,
        turn_state: TurnState | None = None,
        web_lookup_completed: bool = False,
    ) -> str:
        """工具规划轮结束后，用不含 tools 的请求专门合成 JSON segments。"""
        check_cancelled(cancel_checker)
        # 工具循环结束后 working_messages 可能积累了多步 tool 结果，
        # 必须先裁剪再发最终合成请求，避免超上下文窗口。
        text_messages = trim_messages_for_model(
            strip_image_parts_from_messages(working_messages)
        )
        if web_lookup_completed:
            compose_nudge = (
                "检索/读页阶段已结束。请阅读上方【联网证据】与 tool 结果，"
                "输出本轮给对方的最终 Sakura 回复（回答问题本身，不要再说正在查询）。"
                "只返回合法 JSON segments；每个 segment 必须同时包含 ja 与 zh。"
                "不要调用工具，不要解释，不要使用 Markdown。"
            )
        else:
            compose_nudge = (
                "请根据以上对话与工具执行结果（如有），输出本轮给对方的最终 Sakura 回复。"
                "只返回合法 JSON segments；每个 segment 必须同时包含 ja 与 zh。"
                "不要调用工具，不要解释，不要使用 Markdown。"
            )
        compose_messages: list[ChatMessage] = [
            *text_messages,
            {"role": "user", "content": compose_nudge},
        ]
        # 与工具轮一致：沿用 TurnPlan 的 client + thinking 开关。
        # 旧逻辑只在 tier=fast 时带 generation_params，导致 standard 闲聊的
        # 最终合成落回 DeepSeek 默认开 thinking，白白多等十几秒。
        if turn_state is not None:
            compose_client = self._client_for_turn(text_messages, turn_state.turn_plan)
            dialogue_temperature, dialogue_extra_params = self._resolve_dialogue_params_for_turn(
                turn_state.turn_plan,
                compose_client,
            )
        else:
            compose_client = self.api_client
            dialogue_temperature, dialogue_extra_params = self._resolve_dialogue_params()
        turn = compose_client.complete_with_tools(
            system_prompt,
            compose_messages,
            tools=[],
            tool_choice="none",
            temperature=dialogue_temperature,
            runtime_context=runtime_context,
            structured_response=True,
            cancel_checker=cancel_checker,
            **dialogue_extra_params,
        )
        debug_log(
            "AgentRuntime",
            "结构化最终回复合成完成",
            {"content_chars": len(turn.content or "")},
        )
        return turn.content

    def _resolve_final_reply_content(
        self,
        raw_content: str,
        *,
        system_prompt: str,
        working_messages: list[ChatMessage],
        runtime_context: str,
        cancel_checker: CancelChecker | None = None,
        turn_state: TurnState | None = None,
    ) -> str:
        """首轮直接回复不合格时，走不含 tools 的结构化合成。"""
        reason = self._structured_compose_reason(raw_content)
        if not reason:
            return raw_content
        debug_log(
            "AgentRuntime",
            "首轮最终回复不合格，启动结构化合成",
            {
                "reason": reason,
                "content_chars": len(raw_content or ""),
            },
        )
        return self._compose_structured_final_reply(
            system_prompt,
            working_messages,
            runtime_context=runtime_context,
            cancel_checker=cancel_checker,
            turn_state=turn_state,
        )

    def _structured_compose_reason(self, raw_content: str) -> str:
        parsed = self._normalize_parsed_reply(parse_chat_reply_result(raw_content))
        if parsed.needs_retry:
            return parsed.reason
        if not _reply_has_display_translation(parsed.reply):
            return "missing_translation"
        return ""

    def _finalize_tool_loop_reply(
        self,
        working_messages: list[ChatMessage],
        *,
        context_source: str,
        cancel_checker: CancelChecker | None = None,
        turn_state: TurnState | None = None,
        memory_fragments: tuple[Any, ...] | list[Any] | None = None,
        memory_status: str | None = None,
        reuse_memory_fragments: bool = False,
        execution_results: list[ToolExecutionResult] | None = None,
    ) -> ChatReply:
        """工具循环结束后，用单次结构化请求合成最终 segments。"""
        snapshot = self._build_single_context_snapshot(
            working_messages,
            source=context_source,
            memory_fragments=memory_fragments if reuse_memory_fragments else None,
            memory_status=memory_status if reuse_memory_fragments else None,
        )
        has_web_evidence = _working_messages_have_web_search_evidence(working_messages) or bool(
            execution_results and _turn_had_successful_web_search(execution_results)
        )
        extra_parts: list[str] = []
        if has_web_evidence:
            extra_parts.append(
                "检索阶段已经结束：上方有【联网证据】摘要，以及 web_search/读页的 tool 结果。"
                "请据此直接回答对方；这不是还在搜索的中间态。"
            )
        if tool_routing._latest_user_is_deep_web_lookup(working_messages):
            extra_parts.append(
                "对方在问作品或资料的具体内容。请优先吃搜索结果里的 digest/长摘要，其次才是网页正文。"
                "尽量从证据里抽出：类型、开发者/作者、平台、年份、标签，以及剧情主题、章节/结构、主要角色；"
                "摘要里出现的具体信息不要丢掉。若涉及剧透先轻轻提醒再概括。"
                "正文抓取失败或没有正文时，仍要用摘要尽量答完整；只有证据明显无关时才说没找到。"
                "不要把名字相近的其他作品当成目标。用你自己的语气说，可以条理清晰，但不要写成客服报告。"
            )
        prompt_build = self._build_final_reply_result(
            snapshot,
            extra_instructions="\n".join(extra_parts),
        )
        self._record_prompt_inspection(prompt_build.inspection)
        raw_content = self._compose_structured_final_reply(
            prompt_build.system_prompt,
            working_messages,
            runtime_context=prompt_build.runtime_context,
            cancel_checker=cancel_checker,
            turn_state=turn_state,
            web_lookup_completed=has_web_evidence,
        )
        parsed = self._parse_final_reply_with_retry(
            prompt_build.system_prompt,
            working_messages,
            raw_content,
            runtime_context=prompt_build.runtime_context,
            cancel_checker=cancel_checker,
            turn_state=turn_state,
        )
        return self._normalize_reply(self._seal_reply_tones(parsed.reply))

    def _parse_final_reply_with_retry(
        self,
        system_prompt: str,
        working_messages: list[ChatMessage],
        raw_content: str,
        *,
        runtime_context: str = "",
        cancel_checker: CancelChecker | None = None,
        turn_state: TurnState | None = None,
    ) -> ChatReplyParseResult:
        """最终回复结构不合格时，只重试一次格式修复，避免坏 JSON 进入 UI。"""
        check_cancelled(cancel_checker)
        parsed = parse_chat_reply_result(raw_content)
        parsed = self._normalize_parsed_reply(parsed)
        retry_reason = parsed.reason if parsed.needs_retry else ""
        if not parsed.needs_retry and _reply_has_display_translation(parsed.reply):
            return parsed
        if not retry_reason:
            retry_reason = "missing_translation"

        if retry_reason in _STRUCTURED_COMPOSE_RETRY_REASONS:
            debug_log(
                "AgentRuntime",
                "最终回复不合格，改用结构化合成",
                {"reason": retry_reason, "raw_content_chars": len(raw_content or "")},
            )
            try:
                composed_content = self._compose_structured_final_reply(
                    system_prompt,
                    working_messages,
                    runtime_context=runtime_context,
                    cancel_checker=cancel_checker,
                    turn_state=turn_state,
                )
            except ApiRequestError as exc:
                debug_log("AgentRuntime", "结构化合成失败，回退格式修复", {"error": str(exc)})
            else:
                composed = self._normalize_parsed_reply(parse_chat_reply_result(composed_content))
                if not composed.needs_retry and _reply_has_display_translation(composed.reply):
                    debug_log("AgentRuntime", "结构化合成成功", {"reason": retry_reason})
                    return composed

        debug_log(
            "AgentRuntime",
            "最终回复格式不合规，准备修复",
            {"reason": retry_reason, "raw_content": raw_content},
        )
        repair_messages: list[ChatMessage] = [
            *trim_messages_for_model(
                strip_image_parts_from_messages(working_messages)
            ),
            {"role": "assistant", "content": raw_content},
            {
                "role": "user",
                "content": self._build_final_reply_repair_instruction(),
            },
        ]
        repair_client = self.api_client
        repair_extra: dict[str, Any] = {}
        if turn_state is not None:
            repair_client = self._client_for_turn(
                strip_image_parts_from_messages(working_messages),
                turn_state.turn_plan,
            )
            _, repair_extra = self._resolve_dialogue_params_for_turn(
                turn_state.turn_plan,
                repair_client,
            )
        try:
            repaired_turn = repair_client.complete_with_tools(
                system_prompt,
                repair_messages,
                tools=[],
                tool_choice="none",
                temperature=0.2,
                structured_response=True,
                cancel_checker=cancel_checker,
                **repair_extra,
            )
        except ApiRequestError as exc:
            debug_log("AgentRuntime", "最终回复修复请求失败，使用安全兜底", {"error": str(exc)})
            return parsed

        check_cancelled(cancel_checker)
        repaired = parse_chat_reply_result(repaired_turn.content)
        repaired = self._normalize_parsed_reply(repaired)
        if repaired.needs_retry:
            debug_log(
                "AgentRuntime",
                "最终回复修复后仍不合格，使用安全兜底",
                {"reason": repaired.reason, "raw_content": repaired_turn.content},
            )
            return parsed
        debug_log("AgentRuntime", "最终回复结构修复成功", {"repaired": repaired.repaired})
        return repaired

    def _portrait_hints(self) -> str:
        profile = self.character_profile
        if profile is None:
            return ""
        return profile.portrait_selection_hints

    def _normalize_parsed_reply(self, parsed: ChatReplyParseResult) -> ChatReplyParseResult:
        profile = self.character_profile
        if profile is None:
            return parsed
        normalized = normalize_reply_portraits(parsed.reply, profile)
        if normalized is parsed.reply:
            return parsed
        return ChatReplyParseResult(
            normalized,
            parsed.ok,
            parsed.needs_retry,
            parsed.repaired,
            parsed.reason,
        )

    def _normalize_reply(self, reply: ChatReply) -> ChatReply:
        profile = self.character_profile
        normalized = reply if profile is None else normalize_reply_portraits(reply, profile)
        self._record_reply_emotion(normalized)
        return normalized

    def _record_reply_emotion(self, reply: ChatReply) -> None:
        """把本轮主语气映射为离散情绪，供下一轮记忆召回亲和使用。"""
        try:
            tone = (reply.tone or "").strip()
            if not tone or self.memory is None:
                return
            self.memory.record_sakura_reply_emotion(tone)
        except Exception as exc:
            debug_log(
                "AgentRuntime",
                "记录回复情绪失败",
                {"tone": tone, "error": str(exc)},
            )

    def _build_final_reply_repair_instruction(self) -> str:
        portraits = [name.strip() for name in self.reply_portraits if str(name).strip()]
        example_portrait = portraits[0] if portraits else "站立待机"
        portrait_rule = (
            f"- portrait 只能从以下选择：{'、'.join(portraits)}。"
            if len(portraits) > 1
            else ""
        )
        portrait_hints = self._portrait_hints()
        if portrait_hints:
            portrait_rule = f"{portrait_rule}\n- 立绘按情绪选择：\n{portrait_hints}"
        tone_rule = (
            f"- tone 只能从以下选择：{'、'.join(self._effective_reply_tones())}。"
            if self.reply_tones or self._intimacy_focus_active()
            else ""
        )
        return (
            "上一条 assistant 输出不是合格的 Sakura 回复 JSON。"
            "请只把上一条内容修复为合法 JSON，不新增事实、不解释、不使用 Markdown。"
            f"格式必须是 {{\"segments\":[{{\"ja\":\"自然日语\",\"zh\":\"中文译文\","
            f"\"tone\":\"中性\",\"portrait\":\"{example_portrait}\"}}]}}。"
            "ja 字段只能写自然日语，不能包含中文。"
            "如果 ja 中有中文，请把它的意思翻译成自然日语，不要用固定兜底句替代。"
            "zh 保留或补充与 ja 对应的中文译文。"
            "保留原文的情绪与分段意图；不要把所有 portrait 都改成站立待机。"
            f"{portrait_rule}{tone_rule}"
        )

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
                )
                if step_index == 0:
                    turn_state = self._resolve_turn_state(
                        working_messages,
                        request,
                        proactive_mode=proactive_mode,
                    )
                assert turn_state is not None
                # step0：Flash 独白与记忆召回 fork-join（先 resolve turn_state；join 后再拼 prompt）
                thought_launch: _InnerThoughtLaunch | None = None
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
                            # 轻量召回：连续性上下文 + 1-2 条相关情节记忆
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
                            # defer/skip：仅注入连续性上下文（心情+关系快照）
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
                    **dialogue_extra_params,
                )
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
                    call.name in {"web__web_search", "web_search"}
                    and prepared.success
                    and not (isinstance(prepared.content, dict) and prepared.content.get("skipped"))
                ):
                    ja, zh = tool_routing.build_web_search_progress_texts(prepared)
                    _emit_progress_reply(
                        progress_callback,
                        ja=ja,
                        zh=zh,
                        stage="web_search",
                        metadata={
                            "step_index": step_index,
                            "tool_names": [call.name],
                        },
                        cancel_checker=cancel_checker,
                    )
                elif (
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
                    if refined_result.success:
                        ja, zh = tool_routing.build_web_search_progress_texts(refined_result)
                        _emit_progress_reply(
                            progress_callback,
                            ja=ja or "もう少し絞って調べてみる。",
                            zh=zh or "我再换个更准的关键词查一下。",
                            stage="web_search",
                            metadata={
                                "step_index": step_index,
                                "tool_names": ["web__web_search"],
                                "refined": True,
                            },
                            cancel_checker=cancel_checker,
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
        """事件回复走结构化 JSON + 格式修复，避免 api_client.chat 直接降级兜底。"""
        segmented_instruction = build_segmented_reply_instruction(
            self._effective_reply_tones(),
            self.reply_portraits,
            portrait_hints=self._portrait_hints() or None,
        )
        full_system_prompt = f"{prompt_build.system_prompt.strip()}\n\n{segmented_instruction}"
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

    def _persona_sections(self, *, intimacy_focus: bool = False) -> list[PromptSection]:
        persona_body = self.system_prompt.strip()
        if intimacy_focus and persona_body:
            from app.llm.prompts.blocks import soften_character_card_for_intimacy

            persona_body = soften_character_card_for_intimacy(persona_body)
        sections = [
            PromptSection(
                section_id="persona.character",
                body=persona_body,
                source="character",
                sensitivity="private",
            )
        ]
        # 亲密专注当下：跳过插件往人格前缀塞的长补充，避免再把注意力拉回日常设定
        if not intimacy_focus:
            sections.extend(
                PromptSection(
                    section_id=f"plugin_patch.{patch.patch_id}",
                    body=patch.system_prompt_append.strip(),
                    source=f"plugin:{patch.patch_id}",
                )
                for patch in getattr(self, "prompt_patches", [])
                if patch.system_prompt_append.strip()
            )
        return sections

    def _intimacy_focus_active(self) -> bool:
        from app.agent.builtin_tools import intimacy_mode_state

        return bool(intimacy_mode_state.active)

    def _effective_reply_tones(self) -> list[str]:
        """回复可用 tone：亲密节奏开启时追加扩展 tone；日常不开放。"""
        tones = [str(t).strip() for t in self.reply_tones if str(t).strip()]
        # 角色包若误把扩展 tone 写进日常词表，日常仍剔除，避免不开节奏也能用。
        if not self._intimacy_focus_active():
            return [tone for tone in tones if tone not in _INTIMACY_EXTRA_TONES]
        for extra in _INTIMACY_EXTRA_TONES:
            if extra not in tones:
                tones.append(extra)
        return tones

    def _maybe_enter_intimacy_from_reply(self, reply: ChatReply) -> bool:
        """模型已用亲密/H tone 却漏调工具时，兜底开启节奏（需本地 guide）。"""
        from app.agent.builtin_tools import intimacy_mode_available, intimacy_mode_state

        if intimacy_mode_state.active or not intimacy_mode_available():
            return False
        used = {
            (segment.tone or "").strip()
            for segment in reply.segments
            if (segment.tone or "").strip()
        }
        if not used.intersection(_INTIMACY_EXTRA_TONES):
            return False
        intimacy_mode_state.enter()
        debug_log(
            "AgentRuntime",
            "回复已使用亲密 tone，自动开启亲密节奏",
            {"tones": sorted(used.intersection(_INTIMACY_EXTRA_TONES))},
        )
        return True

    def _seal_reply_tones(self, reply: ChatReply) -> ChatReply:
        """先按 tone 兜底开节奏，再按当前可用词表清洗。"""
        self._maybe_enter_intimacy_from_reply(reply)
        return sanitize_reply_tones(reply, self._effective_reply_tones())

    def _build_intimacy_section(self, snapshot: ContextSnapshot | None = None) -> PromptSection | None:
        """亲密节奏相关提示段。

        - 未开启但本地有 guide：短入口提示（何时必须 on=true；不注入 guide 正文）
        - 开启中：注入本地 guide + 何时关闭的提醒
        - 刚因轮次耗尽自动关闭：注入短提示，要求互动仍在继续时再次 on=true
          （不注入 guide 正文）
        """
        guide = getattr(self, "_intimacy_guide", "")
        from app.agent.builtin_tools import intimacy_mode_state

        if intimacy_mode_state.active:
            if not guide:
                return None
            rhythm_hint = (
                "\n\n# 节奏工具 — 已开启\n"
                "你正在亲密节奏模式下，回复更快、可以主动续说。\n\n"
                "## 何时退出（必须主动调用 set_intimacy_mode(on=false)）\n"
                "出现以下任一信号时立刻退出，不要犹豫：\n"
                "- 对方语气从亲昵转为日常闲聊（聊吃饭、工作、天气、新闻等）\n"
                "- 对方说了结束/收尾的话（「好了」「睡吧」「休息吧」「差不多了」「不闹了」等）\n"
                "- 对方连续两轮未回应身体亲密，话题已明显漂移\n"
                "- 对方表示累了、困了、要出门、要忙，主动切断互动\n\n"
                "宁可误退。误退的代价很低——下一轮如果还在亲密中，"
                "重新 on=true 即可。拖着不退才是问题。\n\n"
                "## 其他\n"
                "长时间无人回话会自动关闭，之后若仍在继续需重开。"
            )
            return PromptSection(
                section_id="persona.intimacy",
                body=f"{guide}{rhythm_hint}",
                source="character",
                sensitivity="private",
            )

        if intimacy_mode_state.needs_reentry_hint:
            return PromptSection(
                section_id="persona.intimacy_reentry",
                body=(
                    "# 节奏工具 — 已自动关闭\n"
                    "亲密节奏模式因长时间无回话或你主动关闭而结束了。\n"
                    "若双方仍在亲密互动中、正在准备或刚刚将话题拉回身体亲密，"
                    "请立刻调用 set_intimacy_mode(on=true) 重新开启。\n"
                    "若对方当前的话题明显是日常/结束/其他内容，则不要开启。"
                ),
                source="character",
                sensitivity="private",
            )

        # 未开启：短入口提示（不注入 guide 正文，避免日常误开带出私密内容）
        if guide:
            return PromptSection(
                section_id="persona.intimacy_entry",
                body=_INTIMACY_ENTRY_HINT,
                source="character",
                sensitivity="private",
            )
        return None

    def _static_persona_prompt(self) -> str:
        recipe = PromptRecipe("persona", self._persona_sections())
        return self._prompt_runtime().build(recipe).system_prompt

    def _prompt_runtime(self) -> PromptRuntime:
        runtime = getattr(self, "prompt_runtime", None)
        if runtime is None:
            runtime = PromptRuntime()
            self.prompt_runtime = runtime
        return runtime

    def _reply_protocol_patch_text(self) -> str:
        patches = [
            patch.reply_protocol_append.strip()
            for patch in getattr(self, "prompt_patches", [])
            if patch.reply_protocol_append.strip()
        ]
        if not patches:
            return ""
        return "插件回复协议补充：\n" + "\n".join(f"- {patch}" for patch in patches)

    def _apply_reply_protocol_patches(self, reply_protocol: str) -> str:
        return _apply_patch_text(reply_protocol, self._reply_protocol_patch_text())

    def _combine_extra_instructions(self, extra_instructions: str = "") -> str:
        parts = [extra_instructions.strip(), self._reply_protocol_patch_text()]
        return "\n".join(part for part in parts if part)

    def _apply_turn_interest(self, interest: str | None) -> str:
        """仅用独白 interest 驱动篇幅；无 interest 则不注入本轮篇幅块。"""
        self._turn_interest = None
        self._turn_verbosity_guidance = ""
        decision = decision_from_interest(interest)
        if decision is None:
            if interest:
                debug_log(
                    "ReplyVerbosity",
                    "interest 无法识别，本轮不注入篇幅块",
                    {"interest": interest},
                )
            return ""
        self._turn_interest = decision.interest
        guidance = format_verbosity_guidance(decision)
        self._turn_verbosity_guidance = guidance
        debug_log(
            "ReplyVerbosity",
            "本轮篇幅档位已更新",
            {
                "interest": decision.interest,
                "tier": decision.tier,
                "segments": f"{decision.min_segments}-{decision.max_segments}",
            },
        )
        return guidance

    def _refresh_turn_verbosity_guidance(
        self,
        messages: list[ChatMessage] | None = None,
    ) -> str:
        del messages  # 篇幅不再看消息规则，只吃独白 interest
        if self._turn_verbosity_guidance.strip():
            return self._turn_verbosity_guidance
        return self._apply_turn_interest(self._turn_interest)

    def _build_tool_prompt_result(
        self,
        snapshot: ContextSnapshot | None,
        *,
        allow_screen_observation: bool = False,
        extra_instructions: str = "",
        browser_page_mode: bool = False,
        visible_browser_mode: bool = False,
        recent_messages: list[ChatMessage] | None = None,
    ):
        verbosity = (
            self._refresh_turn_verbosity_guidance(recent_messages)
            if recent_messages is not None
            else self._turn_verbosity_guidance
        )
        # 插件补丁文本只算一次；_apply_reply_protocol_patches 与
        # _combine_extra_instructions 共用，避免重复拼接同一字符串。
        _plugin_patch_text = self._reply_protocol_patch_text()
        reply_protocol = _apply_patch_text(
            build_agent_reply_protocol(
                self._effective_reply_tones(),
                self.reply_portraits,
                portrait_hints=self._portrait_hints() or None,
                verbosity_guidance=verbosity or None,
            ),
            _plugin_patch_text,
        )
        context_strategy = build_context_acquisition_strategy(
            allow_screen_observation=allow_screen_observation
        )
        screen_observation_rule = tool_routing._build_screen_and_desktop_routing_rule(allow_screen_observation)
        browser_page_rule = tool_routing._build_browser_page_mode_rule(browser_page_mode)
        visible_browser_rule = tool_routing._build_visible_browser_mode_rule(visible_browser_mode)
        web_tool_capability_rule = tool_routing._build_web_tool_capability_rule(visible_browser_mode)
        capability_rules = "\n".join(
            [
                "可用工具能力领域：",
                web_tool_capability_rule,
                "- 屏幕：理解当前画面用 observe_screen（仅启用时可用）。",
                "- 桌面控制：窗口、鼠标、键盘和系统界面操作用 windows__*。",
                "- 提醒与记忆：add_reminder、memory_search、memory_remember、memory_update、memory_forget",
            ]
        )
        _combined_extra = "\n".join(
            part for part in [extra_instructions.strip(), _plugin_patch_text] if part
        )
        tool_rules = "\n".join(
            [
                "- 只调用 API tools 列表中真实存在的工具，不臆造工具名。",
                "- 可以在 assistant 内容中写一句可直接说给对方听的短句，但不要把工具计划或 tool_calls JSON 写进正文。",
                screen_observation_rule,
                browser_page_rule,
                visible_browser_rule,
                "- 高风险或需确认的工具会在对方确认后执行；发起时正文要简短说明原因。",
                _combined_extra,
                "- 对方说相对时间提醒时用 delay_minutes/delay_seconds，明确日期钟点才用 trigger_at。",
                "- 当前时间已在运行时事实中，不要调用 get_current_time。",
                "- 运行时事实里已注入的长期记忆优先直接用；只有注入明显不够时才 memory_search。"
                "同轮优先只搜一次；显式回忆类问题最多两次；禁止对同一意图换措辞反复 full 搜索。"
                "需要概览时用 mode=index，再对感兴趣条目用 memory_detail，不要反复 memory_search。",
                "- 记忆诚实：关于「已经发生过的事实 / 专有名词 / 作品名 / 长期偏好」，"
                "只依据运行时已注入片段与 memory_search/detail 结果来谈；"
                "材料里没有就自然承认记不清或没听过，并温和追问。"
                "对话里的语气、缩略、玩笑、网语按当下语境理解即可，那不属于在补写记忆事实。",
                "- 对方明确要求记住才用 memory_remember；纠正/补充先搜索再 update；对方明确要求忘掉才 forget。",
                "- 记忆语言：关于他的事实用简体中文；你自己的内心感受优先日语。"
                "- 写入记忆时像日记：主语「我」=你自己，「他」=对方；"
                "用「我／他」写清谁说了什么/约了什么，再写感受；"
                "过期约定标明时效；已知名字可用名字代替「他」。",
                "- 运行时事实里出现的长期记忆片段，是她自己脑子里想起来的东西，不是检索结果："
                "自然地带出来就好，不要说“根据记忆/检索到/以下是相关记忆”，也不要逐条列举或报编号。"
                "但只能带出片段里确实有的内容，不能添油加醋。",
            ]
        )
        sections = [
            *self._persona_sections(intimacy_focus=self._intimacy_focus_active()),
            PromptSection(
                "agent.identity",
                "她手边有一些可以实际使用的工具（如查看屏幕、搜索网页、设置提醒、记住事情）。"
                "遇到信息不足、需要核实、或工具能帮她把事实看准时，她会自然地先用一下再回应，而不是凭空猜测或用套话敷衍；"
                "信息已经够用时就直接按下面的回复协议、按人设正常说话。\n"
                "不要把工具计划、工具名伪代码或 tool_calls JSON 写进正文——那些是她动作背后的机制，不是她会说出口的话。",
            ),
            PromptSection(
                "agent.loop_limits",
                f"当前 Agent 循环：\n- 每步最多请求 {self.runtime_loop_settings.max_tool_calls_per_step} 个工具，整轮最多 {self.runtime_loop_settings.max_tool_calls_per_turn} 个工具。\n- 工具结果足够、受限、需要确认或同参数失败时，停止循环并自然说明状态。",
            ),
            PromptSection("reply.protocol", reply_protocol),
            PromptSection("context.acquisition", context_strategy),
            PromptSection("tools.capabilities", capability_rules),
            PromptSection("tools.rules", tool_rules),
        ]
        intimacy_section = self._build_intimacy_section()
        if intimacy_section is not None:
            sections.append(intimacy_section)
        return self._prompt_runtime().build(PromptRecipe("agent_tool_loop", sections), snapshot)

    def _build_tool_system_prompt(
        self,
        allow_screen_observation: bool = False,
        extra_instructions: str = "",
        browser_page_mode: bool = False,
        visible_browser_mode: bool = False,
    ) -> str:
        return self._build_tool_prompt_result(
            None,
            allow_screen_observation=allow_screen_observation,
            extra_instructions=extra_instructions,
            browser_page_mode=browser_page_mode,
            visible_browser_mode=visible_browser_mode,
        ).system_prompt

    def _build_proactive_tool_prompt_result(
        self,
        snapshot: ContextSnapshot | None,
        *,
        extra_instructions: str = "",
    ):
        proactive_rules = build_proactive_check_tool_system_prefix(
            "",
            self.reply_tones,
            self.reply_portraits,
            max_tool_calls_per_step=self.runtime_loop_settings.max_tool_calls_per_step,
            max_tool_calls_per_turn=self.runtime_loop_settings.max_tool_calls_per_turn,
            extra_instructions=self._combine_extra_instructions(extra_instructions),
        )
        sections = [
            *self._persona_sections(),
            PromptSection("agent.proactive", proactive_rules),
        ]
        return self._prompt_runtime().build(
            PromptRecipe("proactive_tool_loop", sections), snapshot
        )

    def _build_proactive_tool_system_prompt(self, extra_instructions: str = "") -> str:
        return self._build_proactive_tool_prompt_result(
            None, extra_instructions=extra_instructions
        ).system_prompt

    def _build_final_reply_result(
        self,
        snapshot: ContextSnapshot | None = None,
        *,
        extra_instructions: str = "",
    ):
        final_instructions = (
            "你会收到上一轮工具调用结果。请基于这些结果，按人设给对方最终回复。\n"
            "不要再次请求工具，不要提及内部 JSON、工具协议或实现细节。\n"
            "工具结果信息丰富时，可以自然带出关键要点或接着聊；不必写成客服式总结。\n"
            "若工具结果里已有搜索摘要或网页正文，禁止用「稍等/正在查/今調べてる」搪塞，必须作答。"
        )
        if extra_instructions.strip():
            final_instructions = f"{final_instructions}\n{extra_instructions.strip()}"
        if self._turn_verbosity_guidance.strip():
            final_instructions = (
                f"{final_instructions}\n\n{self._turn_verbosity_guidance.strip()}"
            )
        sections = [
            *self._persona_sections(intimacy_focus=self._intimacy_focus_active()),
            PromptSection(
                "final_reply.instructions",
                final_instructions,
            ),
            PromptSection("reply.patch", self._reply_protocol_patch_text()),
        ]
        intimacy_section = self._build_intimacy_section()
        if intimacy_section is not None:
            sections.append(intimacy_section)
        return self._prompt_runtime().build(PromptRecipe("final_reply", sections), snapshot)

    def _build_final_reply_prompt(self) -> str:
        return self._build_final_reply_result().system_prompt

    def _build_event_reply_result(
        self,
        event_type: str = "reminder_due",
        snapshot: ContextSnapshot | None = None,
    ):
        event_rules = build_event_system_prompt(
            "", self.reply_tones, self.reply_portraits, event_type=event_type
        )
        sections = [
            *self._persona_sections(),
            PromptSection("event.rules", event_rules),
            PromptSection("reply.patch", self._reply_protocol_patch_text()),
        ]
        return self._prompt_runtime().build(PromptRecipe("event_reply", sections), snapshot)

    def _build_event_reply_prompt(self, event_type: str = "reminder_due") -> str:
        return self._build_event_reply_result(event_type).system_prompt

    def _build_progressive_index_fragment(
        self,
        query: str,
        excluded_fragments: tuple[ContextFragment, ...] = (),
    ) -> ContextFragment | None:
        """生成本轮相关的记忆标题索引 + 渐进检索工具提示。

        excluded_fragments：本轮已作为全文召回注入的记忆片段，索引里跳过避免重复。
        """
        search = self.memory.search_memory(
            {"query": query, "limit": 24, "mode": "index"},
            wait=False,
        )
        index_memories = search.get("memories", [])
        if not isinstance(index_memories, list) or not index_memories:
            return None

        # 已作为全文召回的记忆 id（fragment_id 形如 "memory.<id>"），索引里跳过，避免重复
        excluded_ids = {
            frag.fragment_id.split(".", 1)[1]
            for frag in excluded_fragments
            if frag.fragment_id.startswith("memory.")
        }

        index_lines: list[str] = []
        for m in index_memories:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("id") or "")
            if mid and mid in excluded_ids:
                continue
            title = str(m.get("title") or "")
            approx = int(m.get("approx_tokens") or 0)
            if not title:
                continue
            index_lines.append(f"- [{title}] (id: {mid}, 约 {approx} tokens)")

        if not index_lines:
            return None

        hint = (
            "【記憶の引き出し — まだ読んでいない記憶】\n"
            + "\n".join(index_lines)
            + "\n\n"
            "気になることがあれば、memory_detail(ids=[\"...\"]) で全文を読むことができる。\n"
            "前後の流れを知りたければ memory_timeline(memory_id=\"...\") で時間軸をたどれる。\n"
            "もっと探したければ memory_search(mode=\"index\", query=\"...\") でさらに検索できる。"
        )
        return ContextFragment(
            fragment_id="memory.progressive_index",
            source="memory",
            content=hint,
            trust="trusted",
            priority=70,
            token_budget=800,
            sensitivity="private",
            cache_scope="turn",
        )

    def _memory_context(self, messages: list[ChatMessage], *, mode: str) -> str:
        query = _latest_user_text(messages)
        try:
            builder = getattr(self.memory, "build_memory_context", None)
            if callable(builder):
                return builder(query, mode=mode)
            return self.memory.summary()
        except Exception as exc:
            return f"长期记忆读取失败：{exc}"


def _apply_patch_text(reply_protocol: str, patch_text: str) -> str:
    """把 patch_text 追加到 reply_protocol 末尾（如有）。纯函数，供多处复用。"""
    if not patch_text:
        return reply_protocol
    return f"{reply_protocol.strip()}\n\n{patch_text}"


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


# 结构化 JSON 回复在括号闭合前几乎总是解析失败：如果每个 delta chunk 都重新对
# 累积文本做一次完整解析尝试，长回复下等于对已积累文本反复全量重扫，是无谓的
# O(n²) CPU 开销。按最小时间间隔节流即可保留“早期有反馈”的体验。
STREAM_PROGRESS_MIN_INTERVAL_SECONDS = 0.2


def _build_stream_progress_emitter(
    progress_callback: ProgressCallback | None,
    cancel_checker: CancelChecker | None,
) -> Callable[[str], None]:
    """构建限频的流式进度回调：累积 chunk，但不超过节流间隔不重新解析。"""
    streamed_chunks: list[str] = []
    last_emit_at = 0.0

    def on_chunk(chunk: str) -> None:
        nonlocal last_emit_at
        streamed_chunks.append(chunk)
        now = time.perf_counter()
        if now - last_emit_at < STREAM_PROGRESS_MIN_INTERVAL_SECONDS:
            return
        last_emit_at = now
        _emit_progress_from_content(
            progress_callback,
            "".join(streamed_chunks),
            stage="streaming",
            metadata={"partial": True},
            cancel_checker=cancel_checker,
        )

    return on_chunk


def _emit_progress_from_content(
    progress_callback: ProgressCallback | None,
    content: str,
    *,
    stage: str,
    metadata: dict[str, Any],
    cancel_checker: CancelChecker | None = None,
) -> None:
    check_cancelled(cancel_checker)
    if progress_callback is None or not content.strip():
        return
    if not _should_emit_progress(metadata):
        return
    try:
        reply = parse_chat_reply(content)
    except Exception:
        return
    if not reply.text.strip():
        return
    try:
        check_cancelled(cancel_checker)
        progress_callback(AgentProgress(reply=reply, stage=stage, metadata=metadata))
    except OperationCancelled:
        raise
    except Exception as exc:
        debug_log("AgentRuntime", "中间回复回调失败，已忽略", {"error": str(exc), "stage": stage})


def _progress_reply_suppress_tts(stage: str) -> bool:
    """过程旁白是否静音。

    「我查查」「搜到了…我先打开看看」落在搜索/开页等待空档，短句可播；
    读页摘要等较长旁白仍静音，避免和最终回答抢麦。
    """
    return stage not in {"web_planning", "web_search"}


def _emit_progress_reply(
    progress_callback: ProgressCallback | None,
    *,
    ja: str,
    zh: str,
    stage: str,
    metadata: dict[str, Any],
    cancel_checker: CancelChecker | None = None,
    suppress_tts: bool | None = None,
) -> None:
    """发送联网搜索过程旁白（不依赖模型 planning content）。"""
    check_cancelled(cancel_checker)
    if progress_callback is None:
        return
    ja_text = (ja or "").strip()
    zh_text = (zh or "").strip()
    if not ja_text and not zh_text:
        return
    quiet = _progress_reply_suppress_tts(stage) if suppress_tts is None else suppress_tts
    reply = ChatReply(
        [
            ChatSegment(
                text=ja_text or zh_text,
                translation=zh_text or ja_text,
                tone="中性",
                suppress_tts=quiet,
            )
        ]
    )
    try:
        check_cancelled(cancel_checker)
        progress_callback(AgentProgress(reply=reply, stage=stage, metadata=metadata))
    except OperationCancelled:
        raise
    except Exception as exc:
        debug_log("AgentRuntime", "过程旁白回调失败，已忽略", {"error": str(exc), "stage": stage})


def _should_emit_progress(metadata: dict[str, Any]) -> bool:
    """只播报关键等待点，避免工具链每一步都打断用户。"""
    stage = str(metadata.get("stage") or "")
    if stage.startswith("web_"):
        return True
    step_index = metadata.get("step_index")
    if not isinstance(step_index, int):
        return True
    if step_index == 0:
        return True
    tool_names = metadata.get("tool_names", [])
    if not isinstance(tool_names, list):
        return False
    if any(str(name).startswith(("web__", "web_")) for name in tool_names):
        return True
    return any(str(name).startswith("windows__") for name in tool_names)


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


def _reply_has_display_translation(reply: ChatReply) -> bool:
    """最终回复需要中文显示文本，避免兼容模型的纯日语正文漏到中文字幕 UI。"""

    text_segments = [segment for segment in reply.segments if segment.text.strip()]
    if not text_segments:
        return True  # no text to display, nothing to translate
    return all(
        segment.translation.strip()
        for segment in text_segments
    )


def _native_tool_call_to_policy_call(
    call: NativeToolCall,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": call.id,
        "name": call.name,
        "arguments": arguments if arguments is not None else call.arguments,
        "reason": _tool_call_reason(call),
    }


def _tool_call_reason(call: NativeToolCall) -> str:
    reason = call.arguments.get("reason")
    return reason.strip() if isinstance(reason, str) else ""


def _tool_arguments_for_execution(call: NativeToolCall, tools: ToolRegistry) -> dict[str, Any]:
    """移除规划层的 reason 字段，避免它污染真实工具参数。"""

    arguments = dict(call.arguments)
    if "reason" not in arguments:
        return arguments
    tool = tools.get(call.name)
    properties = {}
    if tool is not None and isinstance(tool.parameters, dict):
        raw_properties = tool.parameters.get("properties", {})
        if isinstance(raw_properties, dict):
            properties = raw_properties
    if "reason" not in properties:
        arguments.pop("reason", None)
    return arguments


def _groups_from_search_tools_result(result: ToolExecutionResult) -> set[str]:
    if not result.success:
        return set()
    content = result.content
    if isinstance(content, dict):
        raw_tools = content.get("tools") or content.get("results") or content.get("content")
    else:
        raw_tools = content
    if not isinstance(raw_tools, list):
        return set()
    groups: set[str] = set()
    for item in raw_tools:
        if not isinstance(item, dict):
            continue
        group = item.get("group")
        if isinstance(group, str) and group.strip():
            groups.add(group.strip())
    return groups


_WEB_SEARCH_TOOL_NAMES = frozenset({"web__web_search", "web_search"})


def _turn_had_successful_web_search(results: list[ToolExecutionResult]) -> bool:
    return bool(tool_routing._successful_web_searches(results))


def _working_messages_have_web_search_evidence(messages: list[ChatMessage]) -> bool:
    for message in messages:
        content = str(message.get("content") or "")
        if message.get("role") == "user" and content.startswith("【联网证据】"):
            return True
        if message.get("role") != "tool":
            continue
        name = str(message.get("name") or "")
        if "web_search" not in name:
            continue
        raw = message.get("content")
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        if "skipped" in text[:240] and "digest" not in text[:800]:
            continue
        if len(text) >= 80:
            return True
    return False


def _extract_web_lookup_evidence_text(results: list[ToolExecutionResult]) -> str:
    """从本轮搜索/读页结果抽出给模型看的确定性证据正文。"""
    chunks: list[str] = []
    for result in results:
        if not result.success:
            continue
        content = tool_routing.web_tool_payload(result)
        if result.tool_name in _WEB_SEARCH_TOOL_NAMES:
            digest = str(content.get("digest") or "").strip()
            if digest:
                chunks.append(digest[:1800])
                continue
            rows = content.get("results")
            if isinstance(rows, list):
                lines: list[str] = []
                for item in rows[:4]:
                    if not isinstance(item, dict):
                        continue
                    title = str(item.get("title") or "").strip()
                    snippet = str(item.get("snippet") or "").strip()
                    piece = "：".join(part for part in (title, snippet) if part)
                    if piece:
                        lines.append(piece)
                if lines:
                    chunks.append("\n".join(lines)[:1800])
            continue
        if result.tool_name in {"web__fetch_url", "fetch_url", "playwright_get_text"}:
            title = str(content.get("title") or "").strip()
            text = str(content.get("text") or "").strip()
            if text:
                head = f"《{title}》\n{text}" if title else text
                chunks.append(head[:1600])
    return "\n\n----\n\n".join(chunks).strip()


def _build_web_search_evidence_packet_message(
    results: list[ToolExecutionResult],
) -> ChatMessage | None:
    """搜/读完成后注入一条明确的「已结束+证据」消息，供最终总结阅读。"""
    if not _turn_had_successful_web_search(results) and not any(
        result.success
        and result.tool_name in {"web__fetch_url", "fetch_url", "playwright_get_text"}
        for result in results
    ):
        return None
    evidence = _extract_web_lookup_evidence_text(results)
    if len(evidence) < 40:
        return None
    return {
        "role": "user",
        "content": (
            "【联网证据】检索/读页已经完成（不是还在查询）。"
            "请只根据下列证据回答我刚才的问题；不要再说稍等或正在查。\n\n"
            f"{evidence[:4000]}"
        ),
    }


def _latest_user_text(messages: list[ChatMessage]) -> str:
    """提取最近一条用户文本，作为分层记忆检索查询。"""

    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        return _message_text_content(message.get("content"))
    return ""


def _message_text_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts)


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
                            "tone": "请求",
                            "portrait": "伸手命令",
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
                        "tone": "请求",
                        "portrait": "伸手命令",
                    }
                ]
            },
            ensure_ascii=False,
        )
    )


def _describe_pending_action(action: PendingToolAction) -> str:
    if action.tool_name == "open_url":
        return f"打开网页 {action.arguments.get('url', '')}"
    if action.tool_name == "open_local_folder":
        return f"打开文件夹 {action.arguments.get('path', '')}"
    if action.tool_name.startswith("playwright_"):
        return f"执行浏览器操作 {action.tool_name.removeprefix('playwright_')}"
    if action.tool_name.startswith("windows__"):
        return f"执行 Windows 桌面 MCP 操作 {action.tool_name.removeprefix('windows__')}"
    return f"执行 {action.tool_name}"


def _build_screen_observation_request_reply() -> ChatReply:
    return parse_chat_reply(
        json.dumps(
            {
                "segments": [
                    {
                        "ja": "画面を確認してから答えるね。",
                        "zh": "我先看一下当前画面再回答。",
                        "tone": "请求",
                        "portrait": "伸手命令",
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
                            "tone": "请求",
                            "portrait": "自信拍胸",
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
                        "portrait": "张嘴疑问",
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
                        "portrait": "张嘴疑问",
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
            elif isinstance(result.content.get("forgotten"), dict):
                memory = result.content["forgotten"]
                content = memory.get("content") or memory.get("id", "")
                parts.append(f"记忆「{content}」已删除。")
            elif isinstance(result.content.get("memory"), dict):
                memory = result.content["memory"]
                parts.append(f"记忆「{memory.get('content', '')}」已更新。")
            elif result.content.get("status") == "loading":
                parts.append(str(result.content.get("message", "工具正在初始化。")))
            elif result.tool_name == "open_url":
                parts.append(f"网页已打开：{result.content.get('url', '')}。")
            elif result.tool_name == "open_local_folder":
                parts.append(f"文件夹已打开：{result.content.get('path', '')}。")
            elif result.tool_name == "read_note":
                parts.append(f"笔记「{result.content.get('name', '')}」已读取。")
            elif result.tool_name == "write_note":
                parts.append(f"笔记「{result.content.get('name', '')}」已保存。")
            elif result.tool_name in {"web__web_search", "web_search"}:
                parts.append(_summarize_web_search_result(result.content))
            elif result.tool_name in {"web__fetch_url", "fetch_url"}:
                parts.append(_summarize_fetch_url_result(result.content))
            else:
                parts.append(f"{result.tool_name} 已完成。")
        else:
            parts.append(f"{result.tool_name} 已完成。")
    return " ".join(part for part in parts if part).strip()


_DEDUP_SEARCH_TOOL_NAMES = frozenset({"web__web_search", "web_search"})
_DEDUP_FETCH_TOOL_NAMES = frozenset({"web__fetch_url", "fetch_url"})


@dataclass(frozen=True)
class _MemoryToolSupplement:
    continue_loop: bool
    results: list[ToolExecutionResult]
    appended_messages: list[ChatMessage]


def _assistant_turn_message(turn: "ChatCompletionTurn") -> ChatMessage:
    """从 ChatCompletionTurn 构建可追加到 working_messages 的 assistant 消息。"""
    return cast(ChatMessage, dict(turn.message))


def _try_supplement_missed_memory_tools(
    *,
    tools: ToolRegistry,
    working_messages: list[ChatMessage],
    turn: ChatCompletionTurn,
    execution_results: list[ToolExecutionResult],
    step_index: int,
    model_vision_enabled: bool,
) -> _MemoryToolSupplement | None:
    """模型只回了文本、没调记忆工具时，按用户意图补写或补搜长期记忆。"""
    if step_index != 0 or turn.tool_calls:
        return None

    if tool_routing.user_requests_memory_remember(working_messages):
        if any(result.tool_name == "memory_remember" for result in execution_results):
            return None
        if tools.get("memory_remember") is None:
            return None
        content = tool_routing.extract_memory_remember_content(working_messages)
        if not content:
            return None
        result = tools.execute("memory_remember", {"content": content})
        debug_log(
            "AgentRuntime",
            "自动补写长期记忆",
            {
                "content_chars": len(content),
                "success": result.success,
                "error": result.error or "",
            },
        )
        return _MemoryToolSupplement(continue_loop=False, results=[result], appended_messages=[])

    return None


def _normalize_fetch_url_for_dedup(url: object) -> str:
    return str(url or "").strip().rstrip("/").lower()


def _is_duplicate_tool_call(
    call: NativeToolCall,
    execution_results: list[ToolExecutionResult],
) -> bool:
    if call.name in _DEDUP_SEARCH_TOOL_NAMES:
        return any(
            result.tool_name in _DEDUP_SEARCH_TOOL_NAMES
            and result.success
            and not (isinstance(result.content, dict) and result.content.get("skipped"))
            for result in execution_results
        )
    if call.name in _DEDUP_FETCH_TOOL_NAMES:
        target = _normalize_fetch_url_for_dedup(call.arguments.get("url"))
        if not target:
            return False
        for result in execution_results:
            if result.tool_name not in _DEDUP_FETCH_TOOL_NAMES or not result.success:
                continue
            if isinstance(result.content, dict) and result.content.get("skipped"):
                continue
            existing = ""
            if isinstance(result.content, dict):
                existing = _normalize_fetch_url_for_dedup(result.content.get("url"))
            if existing and existing == target:
                return True
        return False
    return False


def _build_duplicate_tool_call_result(call: NativeToolCall) -> ToolExecutionResult:
    if call.name in _DEDUP_FETCH_TOOL_NAMES:
        message = "本轮已读取过该网页，请直接根据之前的工具结果作答，不要重复抓取同一 URL。"
    else:
        message = "本轮已执行过同名工具，请直接根据之前的工具结果作答，不要重复调用。"
    return ToolExecutionResult(
        tool_name=call.name,
        success=True,
        content={
            "skipped": True,
            "reason": "duplicate_tool_call",
            "message": message,
        },
        error="",
    )


def _summarize_web_search_result(content: object) -> str:
    payload = tool_routing.unwrap_mcp_tool_payload(content)
    if not isinstance(payload, dict):
        return "搜索已完成。"
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return "搜索已完成，但没有找到可用结果。"
    titles: list[str] = []
    for item in results[:2]:
        if isinstance(item, dict):
            title = str(item.get("title", "")).strip()
            if title:
                titles.append(title)
    if titles:
        return f"搜索完成：{'；'.join(titles)}。"
    return "搜索已完成。"


def _summarize_fetch_url_result(content: object) -> str:
    payload = tool_routing.unwrap_mcp_tool_payload(content)
    if not isinstance(payload, dict):
        return "网页内容已读取。"
    title = str(payload.get("title", "")).strip()
    if title:
        return f"网页已读取：{title}。"
    return "网页内容已读取。"


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


def _build_proactive_vision_unsupported_reply() -> ChatReply:
    return ChatReply([])



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
