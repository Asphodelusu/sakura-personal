"""上下文构建与内心独白 — 从 runtime.py 拆分的 mixin。

消费 self 上由 AgentRuntime.__init__ 设置的状态；跨 mixin 调用经 MRO 解析。
"""

from __future__ import annotations

import json
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable

from app.agent.context_orchestrator import ContextOrchestrator, build_context_request
from app.agent.inner_thought import (
    DEFAULT_INNER_THOUGHT_JOIN_TIMEOUT_SECONDS,
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
from app.agent.local_context import build_media_context_fragment
from app.agent.lore import LoreIndex, build_lore_context_fragment, load_lore_index
from app.agent.memory import MemoryStore
from app.agent.memory_recall import MemoryRecallResult, MemoryRecallService
from app.agent.prompt_builder import _INTIMACY_EXTRA_TONES
from app.agent.sensory_context import build_sensory_impression_fragment
from app.agent.session_state_context import (
    SESSION_DIGEST_INJECT_MAX_RECENT_MESSAGES,
    build_session_state_fragment,
)
from app.core.cancellation import CancelChecker, check_cancelled
from app.core.debug_log import debug_body_enabled, debug_log
from app.llm.api_client import ChatMessage, OpenAICompatibleClient
from app.llm.prompts.types import ContextFragment, ContextRequest, ContextSnapshot, PromptInspection
from app.plugins.models import ContextProviderContribution


@dataclass(frozen=True)
class _InnerThoughtLaunch:
    """step0 与记忆召回并行的内心独白任务句柄。"""

    future: Future[Any]
    executor: ThreadPoolExecutor
    interaction_id: str = ""


@dataclass(frozen=True)
class _MemoryRecallLaunch:
    """step0 与独白并行的记忆召回（含 query 改写）任务句柄。"""

    future: Future[Any]
    executor: ThreadPoolExecutor



def build_relational_drive_fragment(summary: str) -> ContextFragment | None:
    text = str(summary or "").strip()
    if not text:
        return None
    return ContextFragment(
        fragment_id="runtime.relational_drive",
        source="runtime",
        content=f"[短期内在状态]\n{text}",
        trust="trusted",
        priority=87,
        token_budget=140,
        sensitivity="private",
        cache_scope="turn",
        required=False,
    )


class AgentRuntimeContextMixin:
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
        interaction_id = str(getattr(self, "_relationship_drive_turn_id", "") or "").strip()
        if not interaction_id:
            from app.core.interaction import get_interaction_id

            interaction_id = str(get_interaction_id() or "").strip()
        return _InnerThoughtLaunch(
            future=future,
            executor=executor,
            interaction_id=interaction_id,
        )


    def _finalize_inner_thought_worker(
        self,
        launch: _InnerThoughtLaunch | None,
    ) -> None:
        """join Flash worker；仅在主线程写入滑动窗口，并接通 interest→篇幅。"""
        if launch is None:
            return
        settings = self.inner_thought_settings.normalized()
        join_timeout = float(
            getattr(settings, "join_timeout_seconds", DEFAULT_INNER_THOUGHT_JOIN_TIMEOUT_SECONDS)
        )
        result = InnerThoughtResult(text="", interest=None)
        timed_out = False
        try:
            raw = launch.future.result(timeout=join_timeout)
            if isinstance(raw, InnerThoughtResult):
                result = raw
            elif isinstance(raw, str):
                # 兼容旧 mock / 仅返回正文的调用
                result = InnerThoughtResult(text=str(raw or "").strip(), interest=None)
            elif raw is not None:
                result = InnerThoughtResult(text=str(raw).strip(), interest=None)
        except TimeoutError:
            timed_out = True
            debug_log(
                "InnerThought",
                "内心独白 join 超时，已跳过",
                {"join_timeout": join_timeout},
            )
            result = InnerThoughtResult(text="", interest=None)
        except Exception as exc:  # noqa: BLE001 — 独白失败不阻断主链路
            debug_log(
                "InnerThought",
                "内心独白并行任务异常，已跳过",
                {"error": str(exc)},
            )
            result = InnerThoughtResult(text="", interest=None)
        finally:
            # 超时后勿 wait=True，否则仍会被后台 8s HTTP 拖死
            launch.executor.shutdown(wait=not timed_out, cancel_futures=timed_out)
        settle = getattr(self, "_settle_relationship_drive_appraisal", None)
        if callable(settle) and result.drive_appraisal is not None:
            settle(getattr(launch, "interaction_id", "") or "", result.drive_appraisal)
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


    def _launch_memory_recall_worker(
        self,
        request: ContextRequest,
        *,
        light_mode: bool = False,
    ) -> _MemoryRecallLaunch:
        """与独白并行跑 query 改写 + 向量召回。"""
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory-recall")
        future = executor.submit(self.memory_recall.recall, request, light_mode=light_mode)
        debug_log(
            "MemoryRecall",
            "记忆召回已与内心独白并行启动",
            {"light_mode": light_mode},
        )
        return _MemoryRecallLaunch(future=future, executor=executor)


    def _finalize_memory_recall_worker(
        self,
        launch: _MemoryRecallLaunch | None,
        *,
        timeout: float | None = None,
    ) -> MemoryRecallResult | None:
        """join 记忆召回；失败 fail-open 返回 None（由调用方走连续性兜底）。"""
        if launch is None:
            return None
        try:
            if timeout is None:
                raw = launch.future.result()
            else:
                raw = launch.future.result(timeout=timeout)
            if isinstance(raw, MemoryRecallResult):
                return raw
            return None
        except TimeoutError:
            debug_log(
                "MemoryRecall",
                "记忆召回 join 超时，已跳过",
                {"timeout": timeout},
            )
            return None
        except Exception as exc:  # noqa: BLE001 — 召回失败不阻断主链路
            debug_log(
                "MemoryRecall",
                "记忆召回并行任务异常，已跳过",
                {"error": str(exc)},
            )
            return None
        finally:
            launch.executor.shutdown(wait=False, cancel_futures=False)


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
        if getattr(self, "_should_inject_relationship_drive_fragment", lambda: False)():
            drive = build_relational_drive_fragment(self.relationship_drive_summary())
            if drive is not None:
                fragments.append(drive)

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


