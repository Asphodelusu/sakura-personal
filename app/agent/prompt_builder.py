"""提示词组装与亲密节奏 — 从 runtime.py 拆分的 mixin。

消费 self 上由 AgentRuntime.__init__ 设置的状态；不定义 __init__。
_portrait_hints 等留在 AgentRuntime 的方法经 MRO 解析。
"""

from __future__ import annotations

import app.agent.tool_routing as tool_routing
from app.agent.reply_verbosity import decision_from_interest, format_verbosity_guidance
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


_INTIMACY_ENTRY_HINT = (
    "# 节奏工具\n"
    "若本轮双方已同意、正在准备或即将开始身体亲密"
    "（答应一起做、开始靠近/触碰、动手前的准备，或你准备用 tone「亲密」/「H」），"
    "必须先调用 set_intimacy_mode(on=true)，再写回复。"
    "不要等到做到一半才开；准备阶段就要开。"
    "普通暧昧试探、口头调情、尚未准备动手时不要开。"
    "开启后才能使用 tone「亲密」与「H」，并获得身体亲密向的演出引导。"
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
                "## 系统续投信号（重要）\n"
                "对方沉默时，系统可能注入一条 role=system 的续投信号（含「（続けて）」）。\n"
                "那是系统提示，绝不是对方说过的话；不要回答、复述或当成用户发言。\n"
                "收到后续投信号时，以夜乃桜身份自然续写下一句即可。\n\n"
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
                "- 提醒/记忆/原话：add_reminder、memory_*、history_search、history_read",
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
                "- 查原话用 history_search/read（has_more 则 offset 翻页）。",
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


