from __future__ import annotations

from app.agent.context_orchestrator import build_context_request


def test_explicit_current_input_survives_internal_user_nudge() -> None:
    original_input = "帮我查一下今天上海的天气"
    messages = [
        {"role": "user", "content": original_input},
        {"role": "assistant", "content": "我去查一下。"},
        {
            "role": "user",
            "content": "（内部提示：请现在使用 web__web_search 实际查询。）",
        },
    ]

    request = build_context_request(
        messages,
        source="chat",
        mode="normal",
        event_type="",
        step_index=1,
        remaining_steps=2,
        available_tools=("web__web_search",),
        current_input=original_input,
    )

    assert request.current_input == original_input
    assert request.recent_messages[-1].content.startswith("（内部提示")
