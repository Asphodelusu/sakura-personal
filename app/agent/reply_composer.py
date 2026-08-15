"""最终回复合成与修复 — 从 runtime.py 拆分的 mixin。

消费 self 上由 AgentRuntime.__init__ 设置的状态；跨 mixin 调用经 MRO 解析。
"""

from __future__ import annotations

import json
import re
from typing import Any

import app.agent.tool_routing as tool_routing
from app.agent.prompt_builder import _INTIMACY_EXTRA_TONES
from app.agent.tools import ToolExecutionResult
from app.agent.web_evidence import (
    _build_web_search_evidence_packet_message,
    _turn_had_successful_web_search,
    _working_messages_have_web_search_evidence,
)
from app.config.character_loader import normalize_reply_portraits
from app.core.cancellation import CancelChecker, OperationCancelled, check_cancelled
from app.core.debug_log import debug_log
from app.llm.api_client import (
    ApiRequestError,
    ChatCompletionTurn,
    ChatMessage,
    NativeToolCall,
    strip_image_parts_from_messages,
)
from app.llm.chat_reply import (
    ChatReply,
    ChatReplyParseResult,
    _try_load_json,
    parse_chat_reply_result,
)
from app.llm.context_trimming import trim_messages_for_model

# 用户明确要换装/姿态时，才把全量立绘目录塞进提示词
_FULL_PORTRAIT_CATALOG_RE = re.compile(
    r"(换装|换表情|换个表情|立绘|pose|姿势|拍胸|撩发|摸摸|伸手|表情包|换个脸)",
    re.I,
)


_STRUCTURED_COMPOSE_RETRY_REASONS = frozenset({
    # missing_translation：结构化 segments 缺 zh 不再触发二次 Pro；
    # 纯日语无结构正文仍由 _structured_compose_reason 首轮拉起一次合成。
    "missing_segments",
    "invalid_json",
    "empty",
})

# 这些工具的成功结果通常会改变最终答案；规划轮正文写在结果之前，不能直接复用。
_ANSWER_CHANGING_TOOL_NAMES = frozenset({
    "web__web_search",
    "web_search",
    "web__fetch_url",
    "fetch_url",
    "memory_search",
    "memory_detail",
    "observe_screen",
    "windows__screenshot",
    "windows__snapshot",
    "browser__browser_navigate",
    "browser__browser_snapshot",
})

def _reply_has_display_translation(reply: ChatReply) -> bool:
    """是否已有中文译文（字幕可直接显示）。缺 zh 时允许后补，不挡采用。"""

    text_segments = [segment for segment in reply.segments if segment.text.strip()]
    if not text_segments:
        return True  # no text to display, nothing to translate
    return all(
        segment.translation.strip()
        for segment in text_segments
    )


def _reply_has_adoptable_segments(reply: ChatReply) -> bool:
    """日语正文合格即可采用（tone/portrait 可空）；zh 非门槛。"""

    return any(segment.text.strip() for segment in reply.segments)


def _raw_contains_json_object(raw_content: str) -> bool:
    """正文能否抽出 JSON 对象。纯文本回退不得走缺 zh 直通。"""
    data, _repaired = _try_load_json(str(raw_content or ""))
    return isinstance(data, dict)


class AgentRuntimeReplyMixin:
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
        # 解析后已有可采用日语 segments 时直接采用；缺 zh 交给异步字幕翻译。
        if _reply_has_adoptable_segments(parsed.reply) and _raw_contains_json_object(raw_content):
            return ""
        if not _reply_has_display_translation(parsed.reply):
            return "missing_translation"
        return ""


    def _usable_final_reply_content(self, raw_content: str) -> str | None:
        """正文已是可采用的 segments（zh 可缺）则原样返回，否则 None。"""
        text = str(raw_content or "").strip()
        if not text:
            return None
        if self._structured_compose_reason(text):
            return None
        return text


    def _tool_loop_reusable_reply(
        self,
        turn: ChatCompletionTurn | None,
        *,
        execution_results: list[ToolExecutionResult] | None = None,
    ) -> str | None:
        """工具循环结束后若已有可用完整回复，返回其原文以跳过二次 pro 合成。

        - 无 tool_calls 且正文合格：直接采用（防御；正常应在循环内早退）
        - 有 tool_calls 但正文已合格，且没有「会改写答案」的成功工具结果：采用
          （例如边答边 add_todo；省掉 finalize 那一轮 pro）
        - 刚跑过搜索/读页/记忆检索等：正文多半写在结果前，仍走合成
        """
        if turn is None:
            return None
        usable = self._usable_final_reply_content(turn.content or "")
        if usable is None:
            return None
        if not turn.tool_calls:
            return usable
        results = execution_results or []
        if any(
            bool(getattr(result, "success", False))
            and str(getattr(result, "tool_name", "") or "") in _ANSWER_CHANGING_TOOL_NAMES
            for result in results
        ):
            return None
        return usable


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
        # 合格日语 segments 可直接采用；缺 zh 交给异步翻译，不再二次 Pro。
        if not parsed.needs_retry and _reply_has_adoptable_segments(parsed.reply):
            return parsed
        if not retry_reason:
            retry_reason = "missing_segments"

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
                if not composed.needs_retry and _reply_has_adoptable_segments(composed.reply):
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


    def _wants_full_portrait_catalog(self, current_input: str = "") -> bool:
        return bool(_FULL_PORTRAIT_CATALOG_RE.search(str(current_input or "")))


    def _prompt_reply_portraits(self, *, current_input: str = "") -> list[str]:
        """提示词里的 portrait 白名单：默认精简，换装意图时给全量。"""
        profile = self.character_profile
        full_list = [name.strip() for name in self.reply_portraits if str(name).strip()]
        if profile is None:
            return full_list
        if self._wants_full_portrait_catalog(current_input):
            return full_list or profile.portrait_choices
        recent = tuple(getattr(self, "_recent_portraits", ()) or ())
        core = profile.core_portrait_labels(extra=recent)
        return core or full_list or profile.portrait_choices


    def _portrait_hints(self, *, current_input: str = "") -> str:
        profile = self.character_profile
        if profile is None:
            return ""
        labels = self._prompt_reply_portraits(current_input=current_input)
        return profile.build_portrait_selection_hints(
            include_catalog=self._wants_full_portrait_catalog(current_input),
            portrait_labels=labels,
        )


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
        self._remember_recent_portraits(normalized)
        self._record_reply_emotion(normalized)
        return normalized


    def _remember_recent_portraits(self, reply: ChatReply) -> None:
        recent = getattr(self, "_recent_portraits", None)
        if recent is None:
            return
        for segment in reply.segments:
            label = str(getattr(segment, "portrait", "") or "").strip()
            if not label:
                continue
            if recent and recent[-1] == label:
                continue
            recent.append(label)


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
        # 修复轮与主提示一致用精简白名单，避免再灌一整份立绘目录
        portraits = self._prompt_reply_portraits()
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


