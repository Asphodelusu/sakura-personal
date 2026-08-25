from __future__ import annotations

from pathlib import Path

import json
import time
from collections import deque
from threading import Lock
from typing import Any, Callable, cast

from app.agent.context_orchestrator import ContextOrchestrator, build_context_request
from app.agent.memory_recall import MemoryRecallService
from app.agent.prompt_builder import AgentRuntimePromptMixin
from app.agent.reply_composer import AgentRuntimeReplyMixin
from app.agent.context_builder import AgentRuntimeContextMixin
from app.agent.tool_loop import AgentRuntimeToolLoopMixin
from app.agent.memory import MemoryStore
from app.agent.lore import LoreIndex, build_lore_context_fragment, load_lore_index
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
from app.config.character_loader import CharacterProfile, normalize_reply_portraits
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
from app.plugins.models import ContextProviderContribution, PromptPatchContribution

from app.llm.prompts.runtime import PromptRuntime
from app.llm.prompts.types import (
    ContextFragment,
    ContextRequest,
    ContextSnapshot,
    PromptInspection,
)

# ---- 拆分模块 re-export（保持既有 `from app.agent.runtime import ...` 可用）----
from app.agent.progress_emitter import (
    STREAM_PROGRESS_MIN_INTERVAL_SECONDS,
    _build_stream_progress_emitter,
    _emit_progress_from_content,
    _emit_progress_reply,
    _progress_reply_suppress_tts,
    _should_emit_progress,
)
from app.agent.tool_message_builder import (
    _build_confirmed_action_continuation_rules,
    _build_confirmed_action_result_message,
    _build_pending_continuation_messages,
    _build_skipped_after_pending_messages,
    _build_tool_messages_for_result,
    _build_tool_role_message,
    _build_tool_result_image_message,
    _build_tool_results_message,
    _compact_message_for_pending_context,
    _compact_messages_for_pending_context,
    _compact_pending_context_content,
    _deduplicate_preserving_order,
    _extract_image_data_urls_from_value,
    _extract_tool_result_images,
    _format_tool_results_for_model,
    _is_screen_observation_request,
    _mcp_image_item_to_data_url,
    _mcp_image_mime_type,
    _redact_tool_images_from_content,
    _redact_tool_result_for_model,
    _truncate_pending_context_text,
    _truncate_text_for_model,
    _truncate_value_for_model,
    _verify_confirmed_windows_click,
)
from app.agent.web_evidence import (
    _WEB_SEARCH_TOOL_NAMES,
    _build_web_search_evidence_packet_message,
    _extract_web_lookup_evidence_text,
    _latest_user_text,
    _message_text_content,
    _turn_had_successful_web_search,
    _working_messages_have_web_search_evidence,
)
from app.agent.tool_call_utils import (
    _DEDUP_FETCH_TOOL_NAMES,
    _DEDUP_SEARCH_TOOL_NAMES,
    _MemoryToolSupplement,
    _assistant_turn_message,
    _build_duplicate_tool_call_result,
    _groups_from_search_tools_result,
    _is_duplicate_tool_call,
    _native_tool_call_to_policy_call,
    _normalize_fetch_url_for_dedup,
    _tool_arguments_for_execution,
    _tool_call_reason,
    _try_supplement_missed_memory_tools,
)
from app.agent.fallback_replies import (
    _build_fallback_tool_reply,
    _build_pending_action_reply,
    _build_proactive_vision_unsupported_reply,
    _build_screen_observation_request_reply,
    _build_vision_unsupported_reply,
    _describe_pending_action,
    _summarize_fetch_url_result,
    _summarize_tool_results,
    _summarize_web_search_result,
)
from app.agent.event_message_builder import (
    _build_event_messages,
    _build_event_screen_context_image_parts,
    _build_screen_context_image_part,
    _format_event_for_model,
    _normalize_image_detail,
    _redact_event_for_model,
    _redact_screen_context_for_model,
    _sanitize_event_recent_conversation,
    _truncate_event_recent_conversation_content,
)

_INTIMACY_GUIDE_PATH = Path(__file__).resolve().parents[2] / "data" / "intimacy_guide.txt"


def _load_intimacy_guide() -> str:
    try:
        return _INTIMACY_GUIDE_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        return ""




class AgentRuntime(AgentRuntimePromptMixin, AgentRuntimeReplyMixin, AgentRuntimeContextMixin, AgentRuntimeToolLoopMixin):
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
        # 最近用过的立绘，供日常提示词精简白名单（全量仍在 reply_portraits）
        self._recent_portraits: deque[str] = deque(maxlen=8)
        self.character_profile: CharacterProfile | None = None
        self._lore_index: LoreIndex | None = None
        self._lore_index_path: str = ""
        self._turn_verbosity_guidance: str = ""
        self._turn_interest: str | None = None
        self.tools = tools or ToolRegistry()
        self.memory = memory or MemoryStore()
        self.history_store = history_store
        # 工具 handler 闭包此 ref；换角色时 set_history_store 同步更新 .store
        from app.agent.history_tools import HistoryStoreRef

        self.history_store_ref = HistoryStoreRef(history_store)
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
        # 渐进记忆检索：在召回结果后追加标题索引 + 工具提示。
        # 默认 False；bootstrap / 设置保存后经 set_progressive_memory_enabled 覆盖。
        self._progressive_memory = False
        self._intimacy_guide = _load_intimacy_guide()
        from app.config.relationship_initiative import RelationshipInitiativeSettings

        self._relationship_guide = ""
        self._relationship_settings = RelationshipInitiativeSettings().normalized()
        self._relationship_guide_warned = False

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
            from app.config.character_loader import load_relationship_guide

            self._relationship_guide = load_relationship_guide(
                character_profile.relationship_guide_path
            )

    def set_relationship_initiative(self, settings, guide_text: str = "") -> None:
        from app.config.relationship_initiative import RelationshipInitiativeSettings

        self._relationship_settings = (
            settings.normalized()
            if isinstance(settings, RelationshipInitiativeSettings)
            else RelationshipInitiativeSettings().normalized()
        )
        self._relationship_guide = str(guide_text or "").strip()

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
        ref = getattr(self, "history_store_ref", None)
        if ref is not None:
            ref.store = history_store

    def bind_history_store_ref(self, history_ref: Any) -> None:
        """让工具侧 HistoryStoreRef 与 runtime 共用同一 holder（换角色可同步）。"""
        if history_ref is None:
            return
        self.history_store_ref = history_ref
        history_ref.store = self.history_store



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




























