"""P1-A：结构修复快车道。合格/缺 zh 不重打；纯结构失败走最小请求。"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from app.agent.runtime import AgentRuntime
from app.llm.api_client import ChatCompletionTurn, OpenAICompatibleClient
from app.llm.chat_reply import (
    classify_chat_reply_failure,
    structural_repair_is_faithful,
)


PERSONA = "你是 Sakura，一个桌宠助手。"
HISTORY_USER = "synthetic-history-user"
JA_PROSE = "……開いたよ。今日は曇りみたい。"
FENCED_BROKEN = "```json\n{\"segments\":[{\"ja\":\"" + JA_PROSE + "\"}\n```"
LEGAL_JSON = json.dumps(
    {
        "segments": [
            {"ja": JA_PROSE, "zh": "", "tone": "中性", "portrait": "站立待机"}
        ]
    },
    ensure_ascii=False,
)
REWRITTEN_JSON = json.dumps(
    {
        "segments": [
            {"ja": "天気を確認したよ。", "zh": "我确认了天气。", "tone": "中性", "portrait": "站立待机"}
        ]
    },
    ensure_ascii=False,
)
ILLEGAL_TONE_JSON = json.dumps(
    {
        "segments": [
            {"ja": JA_PROSE, "zh": "", "tone": "狂暴", "portrait": "站立待机"}
        ]
    },
    ensure_ascii=False,
)
MISSING_ZH_JSON = json.dumps(
    {
        "segments": [
            {"ja": "おはよう。", "tone": "开心", "portrait": "站立待机"}
        ]
    },
    ensure_ascii=False,
)
QUALIFIED_JSON = json.dumps(
    {
        "segments": [
            {"ja": "おはよう。", "zh": "早安。", "tone": "开心", "portrait": "站立待机"}
        ]
    },
    ensure_ascii=False,
)
SEMANTIC_EMPTY = ""


def _client() -> MagicMock:
    client = MagicMock(spec=OpenAICompatibleClient)
    client.resolve_dialogue_params.return_value = (0.8, {})
    return client


def _turn(content: str) -> ChatCompletionTurn:
    return ChatCompletionTurn(
        content=content,
        tool_calls=[],
        message={"role": "assistant", "content": content},
    )


def _purposes(client: MagicMock) -> list[str]:
    return [
        str(call.kwargs.get("request_purpose") or "")
        for call in client.complete_with_tools.call_args_list
    ]


def _nth_call_text(client: MagicMock, index: int) -> str:
    args, kwargs = client.complete_with_tools.call_args_list[index]
    system = args[0] if args else ""
    messages = args[1] if len(args) > 1 else kwargs.get("messages") or []
    runtime = kwargs.get("runtime_context") or ""
    tools = kwargs.get("tools")
    return json.dumps(
        {"system": system, "messages": messages, "runtime": runtime, "tools": tools},
        ensure_ascii=False,
    )


def test_classify_distinguishes_ok_missing_zh_envelope_and_semantic() -> None:
    assert classify_chat_reply_failure(QUALIFIED_JSON) == "ok"
    assert classify_chat_reply_failure(MISSING_ZH_JSON) == "ok"
    assert classify_chat_reply_failure(JA_PROSE) == "envelope"
    assert classify_chat_reply_failure(FENCED_BROKEN) == "envelope"
    assert classify_chat_reply_failure('{"note":"no segments here"}') == "schema"
    assert classify_chat_reply_failure(SEMANTIC_EMPTY) == "semantic"


def test_faithful_repair_rejects_rewrite_reorder_and_illegal_enum() -> None:
    assert structural_repair_is_faithful(
        JA_PROSE,
        LEGAL_JSON,
        allowed_tones=["中性", "开心"],
        allowed_portraits=["站立待机"],
    )
    assert not structural_repair_is_faithful(
        JA_PROSE,
        REWRITTEN_JSON,
        allowed_tones=["中性"],
        allowed_portraits=["站立待机"],
    )
    assert not structural_repair_is_faithful(
        JA_PROSE,
        ILLEGAL_TONE_JSON,
        allowed_tones=["中性"],
        allowed_portraits=["站立待机"],
    )
    swapped = json.dumps(
        {
            "segments": [
                {"ja": "今日は曇りみたい。", "tone": "中性", "portrait": "站立待机"},
                {"ja": "……開いたよ。", "tone": "中性", "portrait": "站立待机"},
            ]
        },
        ensure_ascii=False,
    )
    assert not structural_repair_is_faithful(
        JA_PROSE,
        swapped,
        allowed_tones=["中性"],
        allowed_portraits=["站立待机"],
    )
    hallucinated_translation = json.dumps(
        {
            "segments": [
                {
                    "ja": JA_PROSE,
                    "zh": "模型擅自生成的翻译",
                    "tone": "中性",
                    "portrait": "站立待机",
                }
            ]
        },
        ensure_ascii=False,
    )
    assert not structural_repair_is_faithful(
        JA_PROSE,
        hallucinated_translation,
        allowed_tones=["中性"],
        allowed_portraits=["站立待机"],
    )


def test_qualified_and_missing_zh_do_not_start_structural_repair() -> None:
    client = _client()
    client.complete_with_tools.side_effect = [_turn(QUALIFIED_JSON)]
    AgentRuntime(client, PERSONA).handle_user_message(
        [{"role": "user", "content": "synthetic-user-hi"}]
    )
    assert client.complete_with_tools.call_count == 1
    assert _purposes(client) == ["initial"]

    client = _client()
    client.complete_with_tools.side_effect = [_turn(MISSING_ZH_JSON)]
    result = AgentRuntime(client, PERSONA).handle_user_message(
        [{"role": "user", "content": "synthetic-user-hi"}]
    )
    assert client.complete_with_tools.call_count == 1
    assert result.reply.segments[0].translation == ""
    assert "structural_repair" not in _purposes(client)
    assert "semantic_compose" not in _purposes(client)


def test_truncated_json_skips_structural_repair() -> None:
    runtime = AgentRuntime(_client(), PERSONA)

    assert not runtime._can_attempt_structural_repair(  # type: ignore[attr-defined]
        '{"segments":[{"ja":"時間だよ","zh":"到时间了"'
    )


def test_plain_japanese_uses_minimal_structural_repair() -> None:
    client = _client()
    client.complete_with_tools.side_effect = [_turn(JA_PROSE), _turn(LEGAL_JSON)]
    runtime = AgentRuntime(client, PERSONA)
    result = runtime.handle_user_message(
        [
            {"role": "user", "content": HISTORY_USER},
            {"role": "assistant", "content": "synthetic-history-assistant"},
            {"role": "user", "content": "synthetic-user-hi"},
        ]
    )

    assert result.reply.segments[0].text == JA_PROSE
    assert _purposes(client) == ["initial", "structural_repair"]
    repair_blob = _nth_call_text(client, 1)
    assert PERSONA not in repair_blob
    assert HISTORY_USER not in repair_blob
    assert "synthetic-history-assistant" not in repair_blob
    assert "memory_search" not in repair_blob
    assert "synthetic-runtime" not in repair_blob
    assert JA_PROSE in repair_blob
    assert "不得改写" in repair_blob or "改写日文" in repair_blob


def test_rewritten_or_illegal_repair_falls_back_to_semantic_compose() -> None:
    client = _client()
    client.complete_with_tools.side_effect = [
        _turn(JA_PROSE),
        _turn(REWRITTEN_JSON),
        _turn(LEGAL_JSON),
    ]
    AgentRuntime(client, PERSONA).handle_user_message(
        [{"role": "user", "content": "synthetic-user-hi"}]
    )
    assert _purposes(client) == ["initial", "structural_repair", "semantic_compose"]


def test_tool_result_that_changes_answer_stays_on_semantic_compose() -> None:
    client = _client()
    runtime = AgentRuntime(client, PERSONA)
    runtime._compose_structural_repair = MagicMock(  # type: ignore[attr-defined]
        side_effect=AssertionError("tool evidence must not use structural repair")
    )
    runtime._compose_structured_final_reply = MagicMock(return_value=LEGAL_JSON)
    parsed = runtime._finalize_tool_loop_reply(
        [
            {"role": "user", "content": "synthetic-user-search"},
            {
                "role": "assistant",
                "content": JA_PROSE,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "memory_search", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "synthetic-tool-evidence"},
        ],
        context_source="chat",
        execution_results=[
            MagicMock(success=True, tool_name="memory_search"),
        ],
    )
    runtime._compose_structural_repair.assert_not_called()
    runtime._compose_structured_final_reply.assert_called()
    assert parsed.segments[0].text == JA_PROSE


def test_full_context_last_repair_is_not_labeled_structural_repair() -> None:
    client = _client()
    client.complete_with_tools.return_value = _turn(LEGAL_JSON)
    runtime = AgentRuntime(client, PERSONA)
    runtime._try_structural_repair = MagicMock(return_value=None)  # type: ignore[attr-defined]
    runtime._compose_structured_final_reply = MagicMock(return_value="")

    runtime._parse_final_reply_with_retry(
        PERSONA,
        [{"role": "user", "content": HISTORY_USER}],
        "",
        runtime_context="synthetic-runtime",
    )

    last_call = client.complete_with_tools.call_args_list[-1]
    assert last_call.kwargs.get("request_purpose") == "semantic_compose"
    assert HISTORY_USER in json.dumps(last_call.args[1], ensure_ascii=False)
