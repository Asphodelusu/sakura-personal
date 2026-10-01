"""Personal cards keep identity first and behavior guards after the reply protocol."""

import json
from unittest.mock import MagicMock

from app.agent.runtime import AgentRuntime
from app.agent.tools import Tool, ToolRegistry
from app.llm.api_client import ChatMessage, NativeToolCall, OpenAICompatibleClient
from app.llm.prompts.personal_persona import with_desktop_pet_context

CARD = """
【人格设定】
开头身份
## 她怎样存在
存在方式
## 语言与节奏
短句
## 其他叙事
叙事正文

【演出约束】
## 身份与人称
我是测试角色
不要打破角色
""".strip()


def test_layered_card_puts_guards_after_the_reply_protocol() -> None:
    runtime = AgentRuntime(object(), CARD, character_id="fixture", character_name="Fixture")
    prompt = runtime._build_tool_system_prompt()
    assert prompt.index("我是测试角色") < prompt.index("存在方式")
    assert prompt.index("存在方式") < prompt.index("叙事正文")
    assert prompt.index("叙事正文") < prompt.index("segments")
    assert prompt.index("segments") < prompt.rindex("不要打破角色")
    assert "persona.character" not in prompt


def test_plain_card_stays_a_single_leading_block() -> None:
    runtime = AgentRuntime(object(), "SYNTHETIC_PERSONA", character_id="fixture")
    prompt = runtime._build_tool_system_prompt()
    assert prompt.startswith("SYNTHETIC_PERSONA")
    assert "不要打破角色" not in prompt


def test_main_final_and_repair_calls_keep_source_labels() -> None:
    persona = with_desktop_pet_context(
        "SYNTHETIC_CARD",
        system_guards="## 身份与人称\nSYNTHETIC_IDENTITY\n\n## 边界\nSYNTHETIC_BOUNDARY",
    )
    spoken = json.dumps(
        {"segments": [{"ja": "ねえ。", "zh": "嘿。", "tone": "中性"}]},
        ensure_ascii=False,
    )
    client = MagicMock(spec=OpenAICompatibleClient)
    client.resolve_dialogue_params.return_value = (0.8, {})
    client.complete_with_tools.side_effect = [
        MagicMock(content="not-json", tool_calls=[]),
        MagicMock(content=spoken, tool_calls=[]),
    ]
    runtime = AgentRuntime(client, persona, character_id="fixture", character_name="Fixture")
    runtime.handle_user_message([ChatMessage(role="user", content="hello")])
    main_prompt = client.complete_with_tools.call_args_list[0].args[0]
    repair_prompt = client.complete_with_tools.call_args_list[1].args[0]
    for prompt in (main_prompt, repair_prompt):
        assert "【身份锚】" in prompt and "【演出约束】" in prompt and "【人格设定】" in prompt
        assert prompt.index("SYNTHETIC_IDENTITY") < prompt.index("SYNTHETIC_CARD")
        assert prompt.index("SYNTHETIC_CARD") < prompt.index("SYNTHETIC_BOUNDARY")

    tool_client = MagicMock(spec=OpenAICompatibleClient)
    tool_client.resolve_dialogue_params.return_value = (0.8, {})
    tool_client.complete_with_tools.side_effect = [
        MagicMock(
            content="",
            tool_calls=[NativeToolCall(id="call-1", name="my_tool", arguments={})],
        ),
        MagicMock(content=spoken, tool_calls=[]),
    ]
    tool_runtime = AgentRuntime(
        tool_client,
        persona,
        character_id="fixture",
        character_name="Fixture",
        tools=ToolRegistry([
            Tool(
                name="my_tool",
                description="synthetic",
                parameters={"type": "object", "properties": {}, "required": []},
                handler=lambda _args: {"ok": True},
            )
        ]),
    )
    tool_runtime.handle_user_message([ChatMessage(role="user", content="use the tool")])
    final_prompt = tool_client.complete_with_tools.call_args_list[1].args[0]
    assert "【身份锚】" in final_prompt and "【演出约束】" in final_prompt
    assert final_prompt.index("SYNTHETIC_IDENTITY") < final_prompt.index("SYNTHETIC_BOUNDARY")


def test_personal_screen_prompt_allows_silence() -> None:
    runtime = AgentRuntime(object(), "SYNTHETIC_PERSONA", character_id="fixture")
    runtime.configure_personal_reply(personal_style=True)
    prompt = runtime._build_screen_awareness_tool_system_prompt()
    assert '{"silent": true, "segments": []}' in prompt
    assert "必须至少点到一个具体可见对象" not in prompt

    plain = AgentRuntime(object(), "SYNTHETIC_PERSONA", character_id="fixture")
    upstream = plain._build_screen_awareness_tool_system_prompt()
    assert "必须至少点到一个具体可见对象" in upstream
