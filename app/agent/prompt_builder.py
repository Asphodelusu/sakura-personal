"""提示词组装与亲密节奏 — 从 runtime.py 拆分的 mixin。

消费 self 上由 AgentRuntime.__init__ 设置的状态；不定义 __init__。
_portrait_hints 等留在 AgentRuntime 的方法经 MRO 解析。
"""

from __future__ import annotations

import app.agent.tool_routing as tool_routing
from app.agent.reply_verbosity import decision_from_interest, format_verbosity_guidance
from app.agent.web_evidence import _latest_user_text
from app.core.debug_log import debug_log
from app.llm.api_client import ChatMessage
from app.llm.chat_reply import ChatReply, sanitize_reply_tones
from app.llm.prompt_templates import (
    build_agent_reply_protocol,
    build_context_acquisition_strategy,
    build_event_system_prompt,
    build_proactive_check_tool_system_prefix,
)
from app.llm.prompts.runtime import PromptRuntime
from app.llm.prompts.types import ContextSnapshot, PromptRecipe, PromptSection


_INTIMACY_EXTRA_TONES: tuple[str, ...] = ("亲密", "H")


def _intimacy_entry_hint_text() -> str:
    from app.agent.builtin_tools import INTIMACY_ENTER_PHRASE

    return (
        "# 可选亲密导演层\n"
        f"只有对方整句发送「{INTIMACY_ENTER_PHRASE}」时，系统才开启详细 guide、扩展节奏与自动续投；"
        "不会自动开启，也不会因对话自然升温而开启。\n"
        "这只是详细导演层，不是身体接触许可开关。"
        "未开启不限制拥抱、亲吻、前戏或性行为。"
        "稳定恋人关系中，这些互动可以依据既有关系、记忆和双方当下反应自然升温；"
        "出现真实迟疑、退开或拒绝时才停下确认。\n"
        f"未发送「{INTIMACY_ENTER_PHRASE}」不表示关系不足、需要重新认识或默认拒绝；"
        "你仍按自己的判断回应，也可以拒绝。\n"
        "不要猜测或调用 set_intimacy_mode(on=true)。需要结束已开启的导演层时才调用 on=false。"
    )


def _apply_patch_text(reply_protocol: str, patch_text: str) -> str:
    """把 patch_text 追加到 reply_protocol 末尾（如有）。纯函数，供多处复用。"""
    if not patch_text:
        return reply_protocol
    return f"{reply_protocol.strip()}\n\n{patch_text}"



class AgentRuntimePromptMixin:
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
        relationship_section = self._build_relationship_guide_section()
        if relationship_section is not None:
            sections.append(relationship_section)
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


    def _build_relationship_guide_section(self) -> PromptSection | None:
        from app.config.relationship_initiative import (
            RELATIONSHIP_GUIDE_TOKEN_BUDGET,
            expression_bias_guidance,
        )
        from app.core.debug_log import debug_log
        from app.llm.prompts.runtime import estimate_prompt_tokens, truncate_to_token_budget

        settings = getattr(self, "_relationship_settings", None)
        guide = str(getattr(self, "_relationship_guide", "") or "").strip()
        enabled = bool(getattr(settings, "in_turn_enabled", False))
        if not enabled or not guide:
            debug_log(
                "RelationshipInitiative",
                "A 注入",
                {"injected": False, "enabled": enabled, "chars": 0, "tokens": 0},
            )
            return None
        bias = expression_bias_guidance(getattr(settings, "expression_bias", "natural"))
        body, truncated = truncate_to_token_budget(
            f"{guide}\n\n{bias}",
            RELATIONSHIP_GUIDE_TOKEN_BUDGET,
        )
        tokens = estimate_prompt_tokens(body)
        debug_log(
            "RelationshipInitiative",
            "A 注入",
            {
                "injected": True,
                "chars": len(body),
                "tokens": tokens,
                "truncated": truncated,
                "bias": getattr(settings, "expression_bias", "natural"),
            },
        )
        return PromptSection(
            section_id="persona.relationship_guide",
            body=body,
            source="character",
            sensitivity="private",
            cache_scope="static",
            token_budget=RELATIONSHIP_GUIDE_TOKEN_BUDGET,
        )


    def _intimacy_focus_active(self) -> bool:
        from app.agent.builtin_tools import intimacy_mode_state

        return bool(intimacy_mode_state.active)


    def _effective_reply_tones(self) -> list[str]:
        """回复可用 tone：保留角色已配置词表；导演层 active 时补齐缺失的扩展 tone。"""
        tones = [str(t).strip() for t in self.reply_tones if str(t).strip()]
        if self._intimacy_focus_active():
            for extra in _INTIMACY_EXTRA_TONES:
                if extra not in tones:
                    tones.append(extra)
        return tones


    def _seal_reply_tones(self, reply: ChatReply) -> ChatReply:
        """按当前可用词表清洗 tone。开启不由模型 tone 触发。"""
        return sanitize_reply_tones(reply, self._effective_reply_tones())


    def _build_intimacy_section(self, snapshot: ContextSnapshot | None = None) -> PromptSection | None:
        """可选亲密导演层提示段。

        - active：注入本地 guide + 退出提醒；若本轮约定词硬开则追加入口说明
        - 轮次耗尽自动关闭：短重进提示（引导/续投已关，不限制自然回应）
        - 未开启但有 guide：短入口说明（告知硬入口，禁止猜开；不限制亲密行为）
        """
        guide = getattr(self, "_intimacy_guide", "")
        from app.agent.builtin_tools import (
            INTIMACY_ENTER_PHRASE,
            INTIMACY_EXIT_PHRASE,
            intimacy_mode_state,
        )

        if intimacy_mode_state.active:
            if not guide:
                return None
            keyword_note = ""
            if intimacy_mode_state.opened_by_keyword:
                keyword_note = (
                    f"\n\n# 约定入口（本轮已硬开启）\n"
                    f"对方本轮发送了约定词「{INTIMACY_ENTER_PHRASE}」。"
                    "这表示对方请求启用详细 guide 与连续节奏，不表示刚刚取得亲密许可，"
                    "也不创建或升级关系。系统不再机械询问一次相同的模式确认。"
                    "但沉默不代表同意升级；对方迟疑、退开、改变主意或不适时，立即放缓、暂停或确认。"
                    f"安全词「{INTIMACY_EXIT_PHRASE}」或明确拒绝会由系统立即退出。"
                    "不要调用 set_intimacy_mode(on=true)。\n"
                )
            rhythm_hint = (
                f"{keyword_note}\n\n# 节奏 — 已开启\n"
                "你正在可选导演层：回复更快、可以主动续说，并注入详细 guide。\n\n"
                "## 系统续投信号（重要）\n"
                "对方沉默时，系统可能注入一条 role=system 的续投信号（含「（続けて）」）。\n"
                "那是系统提示，绝不是对方说过的话；不要回答、复述或当成用户发言。\n"
                "收到后续投信号时，根据当前状态自然回应；可以放缓、确认或收束，"
                "不要把沉默当成同意升级，也不要仅换说法重复上一句；"
                "若已不再需要详细引导与连续节奏，调用 set_intimacy_mode(on=false)。\n\n"
                "## 何时退出（必须主动调用 set_intimacy_mode(on=false)）\n"
                "出现以下任一信号时立刻退出导演层，不要犹豫：\n"
                "- 对方语气从亲昵转为日常闲聊（聊吃饭、工作、天气、新闻等）\n"
                "- 对方说了结束/收尾/降温的话（「好了」「不闹了」「先这样」"
                "「冷静一下」「睡吧」「休息吧」「差不多了」「聊点别的」等）\n"
                "- 对方连续两轮未回应亲密互动，话题已明显漂移\n"
                "- 对方表示累了、困了、要出门、要忙，主动切断互动\n\n"
                "宁可误退。误退后对方再次发送约定词即可重开。拖着不退才是问题。\n\n"
                "## 其他\n"
                f"长时间无人回话会自动关闭；重开需对方再发「{INTIMACY_ENTER_PHRASE}」。"
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
                    "# 可选导演层 — 引导与自动续投已关闭\n"
                    "详细 guide、扩展节奏与自动续投因长时间无回话或你主动关闭而结束了。\n"
                    f"重开只能等对方再次整句发送约定词「{INTIMACY_ENTER_PHRASE}」；"
                    "不要猜测或调用 set_intimacy_mode(on=true)。\n"
                    "仍按当前关系和意愿自然回应。"
                    "若对方当前话题明显是日常/结束/其他内容，保持日常即可。"
                ),
                source="character",
                sensitivity="private",
            )

        # 未开启：短入口提示（不注入 guide 正文，避免日常误开带出私密内容）
        if guide:
            return PromptSection(
                section_id="persona.intimacy_entry",
                body=_intimacy_entry_hint_text(),
                source="character",
                sensitivity="private",
            )
        return None


    def _assemble_recipe_sections(
        self,
        middle_sections: list[PromptSection],
        *,
        intimacy_focus: bool | None = None,
    ) -> list[PromptSection]:
        """统一 recipe 段落组装：persona 开头 + 中间段 + 可选 intimacy 结尾。

        各 prompt recipe（工具循环 / 最终合成 / 主动事件）都遵循
        「人格段在前、功能段居中、亲密节奏段收尾」的顺序，这里收敛公共部分，
        避免每处重复拼接 persona 与 intimacy 段落。
        """
        if intimacy_focus is None:
            intimacy_focus = self._intimacy_focus_active()
        sections = [
            *self._persona_sections(intimacy_focus=intimacy_focus),
            *middle_sections,
        ]
        intimacy_section = self._build_intimacy_section()
        if intimacy_section is not None:
            sections.append(intimacy_section)
        return sections


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

    def _windows_desktop_tools_available(self) -> bool:
        """当前工具表是否已注册 Windows 桌面 MCP（windows__*）。"""
        tools = getattr(self, "tools", None)
        if tools is None:
            return False
        try:
            return any(
                str(getattr(tool, "name", "")).startswith("windows__")
                for tool in tools.all()
            )
        except Exception:  # noqa: BLE001
            return False

    def _tool_confirmation_rule(self) -> str:
        """与 ToolPermissionPolicy / 完整访问开关对齐的确认说明。"""
        tools = getattr(self, "tools", None)
        free_access = True
        if tools is not None:
            free_access = bool(getattr(tools, "free_access_enabled", True))
        if free_access:
            return (
                "- 完整访问已开启：多数工具会直接执行；"
                "破坏性/高风险操作仍需对方确认。发起高风险时正文简短说明原因。"
            )
        return (
            "- 完整访问已关闭：标记需确认的工具会先经对方确认再执行；"
            "破坏性/高风险始终确认。发起时正文简短说明原因。"
        )


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
        current_input = (
            _latest_user_text(recent_messages) if recent_messages is not None else ""
        )
        prompt_portraits = self._prompt_reply_portraits(current_input=current_input)
        # 插件补丁文本只算一次；_apply_reply_protocol_patches 与
        # _combine_extra_instructions 共用，避免重复拼接同一字符串。
        _plugin_patch_text = self._reply_protocol_patch_text()
        reply_protocol = _apply_patch_text(
            build_agent_reply_protocol(
                self._effective_reply_tones(),
                prompt_portraits,
                portrait_hints=self._portrait_hints(current_input=current_input) or None,
                verbosity_guidance=verbosity or None,
            ),
            _plugin_patch_text,
        )
        context_strategy = build_context_acquisition_strategy(
            allow_screen_observation=allow_screen_observation
        )
        windows_desktop_available = self._windows_desktop_tools_available()
        screen_observation_rule = tool_routing._build_screen_and_desktop_routing_rule(
            allow_screen_observation,
            windows_desktop_available=windows_desktop_available,
        )
        browser_page_rule = tool_routing._build_browser_page_mode_rule(browser_page_mode)
        visible_browser_rule = tool_routing._build_visible_browser_mode_rule(visible_browser_mode)
        web_tool_capability_rule = tool_routing._build_web_tool_capability_rule(visible_browser_mode)
        capability_lines = [
            "可用工具能力领域：",
            web_tool_capability_rule,
            "- 屏幕：理解当前画面用 observe_screen（仅启用时可用）。",
        ]
        if windows_desktop_available:
            capability_lines.append(
                "- 桌面控制：窗口、鼠标、键盘和系统界面操作用 windows__*（当前已启用）。"
            )
        capability_lines.append(
            "- 提醒/记忆：add_reminder、memory_*；原话记录：history_*（非默认，可 search_tools）"
        )
        capability_rules = "\n".join(capability_lines)
        _combined_extra = "\n".join(
            part for part in [extra_instructions.strip(), _plugin_patch_text] if part
        )
        confirmation_rule = self._tool_confirmation_rule()
        tool_rules = "\n".join(
            [
                "- 只调用 API tools 列表中真实存在的工具，不臆造工具名。",
                "- 可以在 assistant 内容中写一句可直接说给对方听的短句，但不要把工具计划或 tool_calls JSON 写进正文。",
                screen_observation_rule,
                browser_page_rule,
                visible_browser_rule,
                confirmation_rule,
                _combined_extra,
                "- 对方说相对时间提醒时用 delay_minutes/delay_seconds，明确日期钟点才用 trigger_at。",
                "- 当前时间已在运行时事实中；不要臆造取时工具。",
                "- 查事实默认只走记忆：已注入片段优先，不够再 memory_search（同轮优先 1 次，显式回忆最多 2 次；"
                "概览用 mode=index + memory_detail）。不要用 history_search 代替记忆检索。",
                "- history_search/read 仅在要逐字原话、按时间翻聊天，或对方明确要查「说过什么/聊天记录」时用"
                "（组 history，可 search_tools；has_more 用 offset）。",
                "- 记忆诚实：既成事实/专名/作品/偏好/是否认识某人，只依据已注入片段与 memory_search/detail；"
                "没有就承认记不清并追问，禁止编造共同经历或熟人关系。语气玩笑按当下语境理解即可。",
                "- 屏幕所见≠私人记忆：屏上角色只能说「刚在你屏幕上看到」；追问「认识吗」先 memory_search，"
                "不够再网页搜；仍无依据就老实说只是看屏看到的。",
                "- 对方明确要求记住才 memory_remember；纠正先搜再 update；明确要求忘掉才 forget。",
                "- 记忆语言：关于他的事实用简体中文；内心感受优先日语。"
                "写入像日记：「我」=你，「他」=对方；过期约定标明时效。",
                "- 运行时注入的长期记忆是她自己想起来的：自然带出，勿说“根据记忆/检索到”，勿逐条报编号；"
                "只能带出片段里确有的内容。",
            ]
        )
        sections = self._assemble_recipe_sections(
            [
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
        )
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
            self._prompt_reply_portraits(),
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
        sections = self._assemble_recipe_sections(
            [
                PromptSection(
                    "final_reply.instructions",
                    final_instructions,
                ),
                PromptSection("reply.patch", self._reply_protocol_patch_text()),
            ]
        )
        return self._prompt_runtime().build(PromptRecipe("final_reply", sections), snapshot)


    def _build_final_reply_prompt(self) -> str:
        return self._build_final_reply_result().system_prompt


    def _build_event_reply_result(
        self,
        event_type: str = "reminder_due",
        snapshot: ContextSnapshot | None = None,
    ):
        event_rules = build_event_system_prompt(
            "",
            self.reply_tones,
            self._prompt_reply_portraits(),
            event_type=event_type,
        )
        sections = [
            *self._persona_sections(),
            PromptSection("event.rules", event_rules),
            PromptSection("reply.patch", self._reply_protocol_patch_text()),
        ]
        return self._prompt_runtime().build(PromptRecipe("event_reply", sections), snapshot)


    def _build_event_reply_prompt(self, event_type: str = "reminder_due") -> str:
        return self._build_event_reply_result(event_type).system_prompt


