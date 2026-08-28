from __future__ import annotations

import json

from app.llm.chat_reply import parse_chat_reply, parse_chat_reply_result


def _segment_fields(reply) -> list[tuple[str, str, str, str, bool]]:
    return [
        (segment.text, segment.tone, segment.translation, segment.portrait, segment.suppress_tts)
        for segment in reply.segments
    ]


def test_structured_action_and_dialogue_segments_stay_unchanged() -> None:
    """Catch accidental rewriting of already-correct silent actions and audible dialogue."""
    raw = json.dumps(
        {
            "segments": [
                {
                    "ja": "（そっと近づく）",
                    "zh": "（轻轻靠近）",
                    "tone": "害羞",
                    "portrait": "害羞浅笑",
                    "suppress_tts": True,
                },
                {
                    "ja": "ただいま。",
                    "zh": "我回来了。",
                    "tone": "害羞",
                    "portrait": "害羞浅笑",
                },
            ]
        },
        ensure_ascii=False,
    )

    reply = parse_chat_reply(raw)

    assert _segment_fields(reply) == [
        ("（そっと近づく）", "害羞", "（轻轻靠近）", "害羞浅笑", True),
        ("ただいま。", "害羞", "我回来了。", "害羞浅笑", False),
    ]


def test_standalone_fullwidth_action_without_suppress_tts_is_silenced() -> None:
    """Catch missing action silence on a standalone full-width parenthesized segment."""
    raw = json.dumps(
        {
            "segments": [
                {
                    "ja": "（そっと近づく）",
                    "zh": "（轻轻靠近）",
                    "tone": "害羞",
                    "portrait": "害羞浅笑",
                }
            ]
        },
        ensure_ascii=False,
    )

    reply = parse_chat_reply(raw)

    assert _segment_fields(reply) == [
        ("（そっと近づく）", "害羞", "（轻轻靠近）", "害羞浅笑", True),
    ]


def test_mixed_dialogue_and_fullwidth_actions_split_in_source_order() -> None:
    """Catch missing action/dialogue split on a single mixed segment."""
    raw = json.dumps(
        {
            "segments": [
                {
                    "ja": "（そっと近づく）ただいま。（頭を下げる）よろしくね。",
                    "zh": "（轻轻靠近）我回来了。（低头）请多关照。",
                    "tone": "害羞",
                    "portrait": "害羞浅笑",
                }
            ]
        },
        ensure_ascii=False,
    )

    reply = parse_chat_reply(raw)

    assert _segment_fields(reply) == [
        ("（そっと近づく）", "害羞", "（轻轻靠近）", "害羞浅笑", True),
        ("ただいま。", "害羞", "我回来了。", "害羞浅笑", False),
        ("（頭を下げる）", "害羞", "（低头）", "害羞浅笑", True),
        ("よろしくね。", "害羞", "请多关照。", "害羞浅笑", False),
    ]


def test_multiline_fallback_splits_on_existing_line_breaks() -> None:
    """Catch a malformed fallback remaining one giant audible bubble."""
    fallback = "\n".join(
        [
            "（そっと近づく）",
            "ただいま。",
            "（頭を下げる）",
            "よろしくね。",
            "待ってたよ。",
        ]
    )

    parsed = parse_chat_reply_result(fallback)

    assert parsed.ok is True
    assert parsed.needs_retry is False
    assert _segment_fields(parsed.reply) == [
        ("（そっと近づく）", "中性", "", "", True),
        ("ただいま。", "中性", "", "", False),
        ("（頭を下げる）", "中性", "", "", True),
        ("よろしくね。", "中性", "", "", False),
        ("待ってたよ。", "中性", "", "", False),
    ]


def test_single_line_dialogue_is_not_split_by_punctuation() -> None:
    """Catch ordinary spoken text being rewritten by punctuation splitting."""
    reply = parse_chat_reply("ただいま。よろしくね。待ってたよ。")

    assert _segment_fields(reply) == [
        ("ただいま。よろしくね。待ってたよ。", "中性", "", "", False),
    ]


def test_unbalanced_fullwidth_parentheses_are_preserved() -> None:
    """Catch guessing on unbalanced or nested full-width parentheses."""
    unbalanced = "（そっと近づく ただいま。"
    nested = "（外の（内側）を見る）まだ動かないよ。"

    unbalanced_reply = parse_chat_reply(unbalanced)
    nested_reply = parse_chat_reply(nested)

    assert _segment_fields(unbalanced_reply) == [
        (unbalanced, "中性", "", "", False),
    ]
    assert _segment_fields(nested_reply) == [
        (nested, "中性", "", "", False),
    ]


def test_unaligned_translation_is_dropped_instead_of_copied() -> None:
    """Catch attaching the original zh blob to every repaired Japanese piece."""
    raw = json.dumps(
        {
            "segments": [
                {
                    "ja": "（そっと近づく）ただいま。",
                    "zh": "我轻轻靠近后说我回来了。",
                    "tone": "害羞",
                    "portrait": "害羞浅笑",
                }
            ]
        },
        ensure_ascii=False,
    )

    reply = parse_chat_reply(raw)

    assert _segment_fields(reply) == [
        ("（そっと近づく）", "害羞", "", "害羞浅笑", True),
        ("ただいま。", "害羞", "", "害羞浅笑", False),
    ]
    assert all(segment.translation == "" for segment in reply.segments)
