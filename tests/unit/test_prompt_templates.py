from __future__ import annotations

from types import SimpleNamespace

from app.agent.actions import AgentEvent, AgentResult
from app.agent.runtime import AgentRuntime
from app.agent.runtime_limits import RuntimeLoopSettings
from app.llm.chat_reply import parse_chat_reply
from app.llm.prompt_templates import (
    build_context_acquisition_strategy,
    build_event_system_prompt,
    build_screen_awareness_check_tool_system_prompt,
    build_screen_awareness_tool_loop_rules,
    build_segmented_reply_instruction,
)
from app.plugins.models import PromptPatchContribution


def _build_proactive_tool_prompt() -> str:
    return build_screen_awareness_check_tool_system_prompt(
        "角色设定",
        ["中性"],
        ["站立待机"],
        memory_summary="无",
        current_time="2026-06-01T12:00:00+08:00",
        step_index=0,
        remaining_steps=2,
        max_tool_calls_per_step=3,
        max_tool_calls_per_turn=6,
    )


def test_proactive_check_tool_prompt_contains_background_web_rules() -> None:
    prompt = _build_proactive_tool_prompt()

    assert "后台 Web 搜索节制" in prompt
    assert "最多 2 次搜索" in prompt
    assert "不能当反向图搜" in prompt
    assert "不搜索私人身份" in prompt


def test_context_acquisition_requires_lookup_before_inventing() -> None:
    text = build_context_acquisition_strategy(allow_screen_observation=True)
    assert "memory_search" in text
    assert "history_search" in text
    assert "禁止编故事" in text or "禁止凭空" in text


def test_proactive_check_tool_prompt_places_web_rules_before_loop_limits() -> None:
    prompt = build_screen_awareness_check_tool_system_prompt(
        "角色设定",
        None,
        None,
        memory_summary="无",
        current_time="2026-06-01T12:00:00+08:00",
        step_index=1,
        remaining_steps=1,
        max_tool_calls_per_step=3,
        max_tool_calls_per_turn=6,
    )

    rules_index = prompt.index("【主动屏幕感知规则】")
    loop_index = prompt.index("当前 Agent 循环：")

    assert rules_index < loop_index


def test_proactive_check_tool_prompt_requires_history_and_image_fusion() -> None:
    prompt = _build_proactive_tool_prompt()

    assert "把 recent_conversation 当作最近完整对话历史" in prompt
    assert "screen_contexts/visual_contexts 当作当前画面" in prompt
    assert "回复必须至少包含一个具体依据" in prompt


def test_reminder_event_prompt_does_not_include_background_web_research_rules() -> None:
    prompt = build_event_system_prompt(
        "角色设定",
        ["中性"],
        ["站立待机"],
        event_type="reminder_due",
    )

    assert "主动屏幕感知后台 Web 搜索规则" not in prompt
    assert "web__web_search" not in prompt
    assert "web__fetch_url" not in prompt


def test_proactive_tool_loop_rules_contains_background_web_research_rules() -> None:
    rules = build_screen_awareness_tool_loop_rules()

    assert "后台 Web 搜索节制" in rules
    assert "最多 2 次搜索" in rules
    assert "2 个网页" in rules


def test_segmented_reply_instruction_can_omit_translation_rules() -> None:
    instruction = build_segmented_reply_instruction(
        ["中性"],
        ["站立待机"],
        include_translation_rules=False,
    )

    assert "禁止中文汉字" not in instruction
    assert "一一对应" not in instruction
    assert "tone 只能从：中性" in instruction


def test_agent_reply_protocol_guides_ja_translation_self_check() -> None:
    instruction = build_segmented_reply_instruction(["中性"], ["站立待机"])

    assert 'ja="原因は Mermaid の構文みたい。"' in instruction
    assert "一一对应" in instruction
    assert "suppress_tts=true" in instruction
    assert "只显示，不朗读" in instruction


def test_segmented_reply_instruction_models_optional_visible_action() -> None:
    instruction = build_segmented_reply_instruction(["中性", "请求"], ["站立待机"])

    assert "（そっと隣に座り、肩を寄せる）" in instruction
    assert '"suppress_tts":true' in instruction
    assert "可观察" in instruction
    assert "纯对白" in instruction
    assert "内心" in instruction


def test_intimacy_entry_prompt_distinguishes_overall_entry_from_ongoing_consent() -> None:
    from app.agent.builtin_tools import INTIMACY_ENTER_PHRASE, intimacy_mode_state

    runtime = object.__new__(AgentRuntime)
    runtime._intimacy_guide = "PRIVATE_GUIDE_MARKER"
    intimacy_mode_state.exit()
    intimacy_mode_state.enter(by_keyword=True)
    intimacy_mode_state.note_user_text(INTIMACY_ENTER_PHRASE)
    try:
        section = runtime._build_intimacy_section()
        assert section is not None
        assert "请求启用详细 guide 与连续节奏" in section.body
        assert "总体许可" not in section.body
        assert "沉默不代表同意升级" in section.body
        assert "安全词「苹果」" in section.body
        assert "不要再口头确认意愿" not in section.body
        assert "推进下一步动作" not in section.body
    finally:
        intimacy_mode_state.exit()


def test_intimacy_tool_copy_uses_safe_word_not_ambiguous_exit_example() -> None:
    from app.agent.builtin_tools import _SET_INTIMACY_MODE_DESCRIPTION

    assert "苹果" in _SET_INTIMACY_MODE_DESCRIPTION
    assert "好了" not in _SET_INTIMACY_MODE_DESCRIPTION
    assert "引导" in _SET_INTIMACY_MODE_DESCRIPTION
    assert "节奏" in _SET_INTIMACY_MODE_DESCRIPTION
    assert "关闭身体亲密" not in _SET_INTIMACY_MODE_DESCRIPTION


def test_prompt_lengths_stay_compact() -> None:
    proactive_tool_prompt = _build_proactive_tool_prompt()
    proactive_event_prompt = build_event_system_prompt(
        "角色设定",
        ["中性"],
        ["站立待机"],
        event_type="proactive_check",
    )
    reminder_prompt = build_event_system_prompt(
        "角色设定",
        ["中性"],
        ["站立待机"],
        event_type="reminder_due",
    )

    assert len(proactive_tool_prompt) < 3600
    assert len(proactive_event_prompt) < 2200
    assert len(reminder_prompt) < 700


def _bare_runtime() -> AgentRuntime:
    """绕过 __init__ 的最小 runtime：补上 __init__ 里初始化、被 prompt 构建读取的属性。"""
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.system_prompt = "角色设定"
    runtime.reply_tones = ["中性"]
    runtime.reply_portraits = ["站立待机"]
    runtime.memory = SimpleNamespace(summary=lambda: "无")
    runtime.runtime_loop_settings = RuntimeLoopSettings()
    runtime._turn_verbosity_guidance = ""
    runtime.character_profile = None
    return runtime


def test_agent_tool_prompt_length_stays_compact() -> None:
    runtime = _bare_runtime()

    prompt = AgentRuntime._build_tool_system_prompt(
        runtime,
        allow_screen_observation=True,
    )

    # 静态前缀不再内联记忆/时间/步数（改由运行时上下文消息注入）。
    # 可见动作合同有意保留一个完整 silent-segment 示例；仍给总前缀设置紧凑上限。
    assert len(prompt) < 3350
    assert prompt.count("主动屏幕感知核心规则") == 0
    assert "长期记忆摘要" not in prompt
    assert "这是第 1 步" not in prompt


def test_agent_tool_prompt_avoids_dead_capability_claims() -> None:
    """提示词不宣称未注册工具 / 不在默认 free-access 下过度承诺确认。"""
    from app.agent.tools import Tool, ToolRegistry

    runtime = _bare_runtime()
    runtime.tools = ToolRegistry([])
    runtime.tools.set_free_access_enabled(True)

    prompt = AgentRuntime._build_tool_system_prompt(
        runtime,
        allow_screen_observation=True,
    )
    assert "get_current_time" not in prompt
    assert "不要臆造取时工具" in prompt or "运行时事实" in prompt
    assert "windows__*" not in prompt or "未启用桌面控制" in prompt
    assert "桌面控制：窗口、鼠标" not in prompt
    assert "完整访问已开启" in prompt
    assert "多数工具会直接执行" in prompt

    runtime.tools.register(
        Tool(
            name="windows__Snapshot",
            description="snap",
            parameters={},
            handler=lambda _a: {},
            group="core",
        )
    )
    runtime.tools.set_free_access_enabled(False)
    with_windows = AgentRuntime._build_tool_system_prompt(
        runtime,
        allow_screen_observation=True,
    )
    assert "桌面控制" in with_windows
    assert "windows__*" in with_windows
    assert "完整访问已关闭" in with_windows


def test_agent_runtime_prompt_patches_apply_to_prompt_builders() -> None:
    runtime = _bare_runtime()
    runtime.prompt_patches = [
        PromptPatchContribution(
            patch_id="demo",
            system_prompt_append="插件系统补丁",
            reply_protocol_append="回复时保留插件约定",
        )
    ]

    tool_prompt = AgentRuntime._build_tool_system_prompt(runtime)
    proactive_prompt = AgentRuntime._build_proactive_tool_system_prompt(runtime)
    event_prompt = AgentRuntime._build_event_reply_prompt(runtime, "reminder_due")
    final_prompt = AgentRuntime._build_final_reply_prompt(runtime)

    for prompt in [tool_prompt, proactive_prompt, event_prompt, final_prompt]:
        assert "插件系统补丁" in prompt
        assert "回复时保留插件约定" in prompt


def test_proactive_event_does_not_pass_duplicate_loop_rules(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_tool_loop(self: AgentRuntime, messages: list[dict], **kwargs: object) -> AgentResult:
        _ = self, messages
        captured.update(kwargs)
        return AgentResult(
            reply=parse_chat_reply(
                '{"segments":[{"ja":"うん。","zh":"嗯。","tone":"中性","portrait":"站立待机"}]}'
            ),
            actions=[],
        )

    monkeypatch.setattr(AgentRuntime, "_run_tool_loop", fake_run_tool_loop)
    runtime = AgentRuntime.__new__(AgentRuntime)

    runtime.handle_event(
        AgentEvent(
            type="proactive_check",
            payload={
                "screen_context_allowed": True,
                "recent_conversation": "用户和 Sakura 的最近对话",
                "visual_contexts": [],
            },
        )
    )

    assert captured["proactive_mode"] is True
    assert "planning_extra_instructions" not in captured
