"""Optional top-level drive_effect: parse fail-open and preserve through adoption."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agent.reply_composer import AgentRuntimeReplyMixin
from app.config.character_loader import normalize_reply_portraits
from app.core.relational_drive import DriveEffect
from app.llm.chat_reply import (
    ChatReply,
    ChatReplyParseResult,
    ChatSegment,
    parse_chat_reply_result,
    sanitize_reply_tones,
)
from app.llm.prompts.recipes import build_agent_reply_protocol, build_segmented_reply_instruction
from tests.unit.test_character_portrait_mapping import _sakura_like_profile


def _payload(*, effect=None) -> dict:
    data = {
        "segments": [
            {"ja": "……好き。", "zh": "……喜欢。", "tone": "亲密"},
        ]
    }
    if effect is not None:
        data["drive_effect"] = effect
    return data


def test_valid_optional_drive_effect_is_parsed() -> None:
    parsed = parse_chat_reply_result(
        json.dumps(
            {
                "segments": [{"ja": "……好き。", "zh": "……喜欢。", "tone": "亲密"}],
                "drive_effect": {"event": "mutual_affection", "strength": "mild"},
            },
            ensure_ascii=False,
        )
    )
    assert parsed.needs_retry is False
    assert parsed.ok is True
    assert parsed.reply.drive_effect is not None
    assert parsed.reply.drive_effect.event == "mutual_affection"
    assert parsed.reply.drive_effect.strength == "mild"
    assert parsed.reply.segments[0].text == "……好き。"


@pytest.mark.parametrize("raw", [None, {}, "bad", {"event": "unknown"}, {"event": "mutual_affection"}])
def test_missing_or_invalid_drive_effect_never_requires_retry(raw) -> None:
    payload = _payload()
    if raw is not None:
        payload["drive_effect"] = raw
    parsed = parse_chat_reply_result(json.dumps(payload, ensure_ascii=False))
    baseline = parse_chat_reply_result(json.dumps(_payload(), ensure_ascii=False))
    assert parsed.needs_retry is False
    assert parsed.ok is True
    assert parsed.reason == baseline.reason
    assert parsed.reply.drive_effect is None
    assert [segment.text for segment in parsed.reply.segments] == [
        segment.text for segment in baseline.reply.segments
    ]


def test_extra_drive_effect_keys_are_ignored_without_retry() -> None:
    parsed = parse_chat_reply_result(
        json.dumps(
            _payload(
                effect={
                    "event": "fulfilled",
                    "strength": "mild",
                    "reason": "secret",
                }
            ),
            ensure_ascii=False,
        )
    )
    assert parsed.needs_retry is False
    assert parsed.reply.drive_effect is None
    assert parsed.reply.segments[0].text == "……好き。"


def test_drive_effect_survives_malformed_action_split() -> None:
    parsed = parse_chat_reply_result(
        json.dumps(
            {
                "segments": [
                    {
                        "ja": "（そっと近づく）ただいま。",
                        "zh": "（轻轻靠近）我回来了。",
                        "tone": "害羞",
                    }
                ],
                "drive_effect": {"event": "mutual_escalation", "strength": "subtle"},
            },
            ensure_ascii=False,
        )
    )
    assert parsed.needs_retry is False
    assert len(parsed.reply.segments) >= 2
    assert parsed.reply.drive_effect == DriveEffect(event="mutual_escalation", strength="subtle")


def test_drive_effect_survives_tone_sanitation_and_portrait_normalization() -> None:
    effect = DriveEffect(event="aftercare", strength="strong")
    reply = ChatReply(
        segments=[ChatSegment("うん。", "en", "嗯。", "")],
        drive_effect=effect,
    )
    sanitized = sanitize_reply_tones(reply, ["中性", "不满", "害羞"])
    assert sanitized.segments[0].tone == "中性"
    assert sanitized.drive_effect == effect
    normalized = normalize_reply_portraits(sanitized, _sakura_like_profile())
    assert normalized.segments[0].portrait
    assert normalized.drive_effect == effect


def test_reply_composer_adoption_preserves_drive_effect() -> None:
    effect = DriveEffect(event="hesitation", strength="mild")
    parsed = ChatReplyParseResult(
        ChatReply(
            segments=[ChatSegment("ちょっと待って。", "中性", "等一下。", "")],
            drive_effect=effect,
        ),
        ok=True,
        needs_retry=False,
    )
    composer = SimpleNamespace(
        character_profile=_sakura_like_profile(),
        _recent_portraits=[],
        memory=None,
        _remember_recent_portraits=lambda _reply: None,
        _record_reply_emotion=lambda _reply: None,
    )
    adopted = AgentRuntimeReplyMixin._normalize_parsed_reply(composer, parsed)
    sealed = AgentRuntimeReplyMixin._normalize_reply(composer, adopted.reply)
    assert adopted.needs_retry is False
    assert adopted.ok is True
    assert adopted.reply.drive_effect == effect
    assert sealed.drive_effect == effect


def test_safe_fallback_does_not_invent_effect() -> None:
    from app.llm.chat_reply import _build_safe_parse_failure_reply

    safe = _build_safe_parse_failure_reply()
    assert safe.drive_effect is None


def test_optional_drive_effect_protocol_is_not_required_in_repair() -> None:
    text = build_segmented_reply_instruction(["中性", "不满"])
    assert "drive_effect" in text
    assert "不确定则省略" in text
    for event in (
        "mutual_affection",
        "mutual_escalation",
        "fulfilled",
        "aftercare",
        "hesitation",
        "stopped",
    ):
        assert event in text
    for strength in ("subtle", "mild", "strong"):
        assert strength in text
    top_level_example = text.split("顶层示例：", 1)[1]
    assert '"segments"' in top_level_example
    assert '"drive_effect"' in top_level_example
    repair = AgentRuntimeReplyMixin._build_final_reply_repair_instruction(
        SimpleNamespace(
            _prompt_reply_portraits=lambda: ["站立待机"],
            _portrait_hints=lambda: "",
            _effective_reply_tones=lambda: ["中性"],
            reply_tones=["中性"],
            _intimacy_focus_active=lambda: False,
        )
    )
    assert "drive_effect" not in repair


def test_agent_reply_protocol_omits_drive_effect_for_opted_out_character() -> None:
    text = build_agent_reply_protocol(
        ["中性"],
        ["站立待机"],
        include_drive_effect=False,
    )

    assert "drive_effect" not in text
