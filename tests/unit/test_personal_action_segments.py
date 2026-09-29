"""Personal action segments: shown in the bubble, never spoken by TTS."""

from __future__ import annotations

import json

from app.agent.runtime import AgentRuntime
from app.llm.chat_reply import parse_chat_reply_result
from app.llm.prompts.recipes import build_agent_reply_protocol

ACTION_JA = "\uff08\u305d\u3063\u3068\u624b\u3092\u63e1\u308b\uff09"
ACTION_ZH = "\uff08\u8f7b\u8f7b\u63e1\u4f4f\u624b\uff09"


def _reply(*segments: dict[str, object], **top: object):
    return parse_chat_reply_result(json.dumps({"segments": list(segments), **top}, ensure_ascii=False))


def _shape(result) -> list[tuple[str, str, bool]]:
    return [(s.text, s.translation, s.suppress_tts) for s in result.reply.segments]


def test_explicit_suppress_tts_is_preserved() -> None:
    result = _reply(
        {"ja": ACTION_JA, "zh": ACTION_ZH, "tone": "\u6e29\u67d4", "suppress_tts": True},
        {"ja": "\u5927\u4e08\u592b\u3060\u3088\u3002", "zh": "\u6ca1\u4e8b\u7684\u3002", "tone": "\u6e29\u67d4"},
    )

    assert _shape(result) == [
        (ACTION_JA, ACTION_ZH, True),
        ("\u5927\u4e08\u592b\u3060\u3088\u3002", "\u6ca1\u4e8b\u7684\u3002", False),
    ]


def test_inline_fullwidth_action_is_split_and_silenced_with_paired_translation() -> None:
    result = _reply({
        "ja": "\uff08\u5fae\u7b11\u3080\uff09\u3042\u308a\u304c\u3068\u3046\u3002",
        "zh": "\uff08\u5fae\u7b11\uff09\u8c22\u8c22\u3002",
        "tone": "\u6e29\u67d4",
    })

    assert _shape(result) == [
        ("\uff08\u5fae\u7b11\u3080\uff09", "\uff08\u5fae\u7b11\uff09", True),
        ("\u3042\u308a\u304c\u3068\u3046\u3002", "\u8c22\u8c22\u3002", False),
    ]


def test_mismatched_translation_shape_does_not_invent_pairs() -> None:
    result = _reply({
        "ja": "\uff08\u5fae\u7b11\u3080\uff09\u3042\u308a\u304c\u3068\u3046\u3002",
        "zh": "\u8c22\u8c22\u3002",
        "tone": "\u6e29\u67d4",
    })

    assert [(s.suppress_tts, s.translation) for s in result.reply.segments] == [(True, ""), (False, "")]


def test_unbalanced_or_nested_brackets_are_left_as_dialogue() -> None:
    for text in ("\uff08\u5fae\u7b11\u3080\u3042\u308a\u304c\u3068\u3046\u3002", "\u3042\uff09\u3044\u3002",
                 "\uff08\u5916\uff08\u5185\uff09\uff09\u3042\u3002"):
        result = _reply({"ja": text, "zh": "", "tone": "\u6e29\u67d4"})
        assert _shape(result) == [(text, "", False)]


def test_drive_effect_and_first_piece_control_survive_the_split() -> None:
    result = _reply(
        {
            "ja": "\uff08\u5fae\u7b11\u3080\uff09\u3042\u308a\u304c\u3068\u3046\u3002",
            "zh": "\uff08\u5fae\u7b11\uff09\u8c22\u8c22\u3002",
            "tone": "\u6e29\u67d4",
            "control": {"expression": "smile"},
        },
        drive_effect={"event": "mutual_affection", "strength": "mild"},
    )

    assert result.reply.drive_effect is not None
    first, second = result.reply.segments
    assert first.control is not None
    assert second.control is None


def test_personal_style_is_opt_in_and_replaces_upstream_segment_rules() -> None:
    upstream = build_agent_reply_protocol(["\u6e29\u67d4"])
    assert "suppress_tts" not in upstream
    assert "35-90" in upstream

    personal = build_agent_reply_protocol(["\u6e29\u67d4"], personal_style=True, character_name="Sakura")
    assert "suppress_tts" in personal and "\uff08" in personal
    assert "35-90" not in personal
    assert "Sakura \u4e0d\u4e60\u60ef\u4e00\u6b21\u8bf4\u5f88\u591a\u8bdd" in personal
    assert "\u4e0d\u5fc5\u4e3a\u4e86\u7a33\u59a5\u9ed8\u8ba4\u6210\u4e2d\u6027" in personal


def test_runtime_offers_personal_style_only_when_enabled() -> None:
    runtime = AgentRuntime(object(), "SYNTHETIC_PERSONA", character_id="fixture", character_name="Sakura")
    assert "suppress_tts" not in runtime._build_tool_system_prompt()

    runtime.configure_personal_reply(personal_style=True)
    prompt = runtime._build_tool_system_prompt()
    assert "suppress_tts" in prompt
    assert "35-90" not in prompt
