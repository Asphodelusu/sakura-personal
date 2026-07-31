"""内心独白：滑动窗口、门控与注入片段。"""

from __future__ import annotations

from app.agent.inner_thought import (
    InnerThoughtSettings,
    InnerThoughtWindow,
    build_inner_thought_fragment,
    format_recent_dialogue,
    should_generate_inner_thought,
    _window_labels,
)


def test_window_keeps_recent_thoughts_in_order() -> None:
    window = InnerThoughtWindow(max_size=3)
    window.push("a")
    window.push("a.next")
    window.push("a.next.next")
    window.push("extra")
    assert window.items() == ("a.next", "a.next.next", "extra")


def test_fragment_includes_sliding_window_for_later_turns() -> None:
    window = InnerThoughtWindow(max_size=6)
    window.push("最初の戸惑い")
    window.push("少し安心した")
    window.push("もっと聞きたい")
    fragment = build_inner_thought_fragment(window, character_name="桜")
    assert fragment is not None
    assert fragment.sensitivity == "private"
    assert fragment.cache_scope == "turn"
    assert "前々" in fragment.content
    assert "最初の戸惑い" in fragment.content
    assert "もっと聞きたい" in fragment.content
    assert "[内心の声]" in fragment.content


def test_should_skip_when_disabled_or_fast_or_missing_client() -> None:
    settings = InnerThoughtSettings(enabled=True, skip_fast_tier=True)
    assert not should_generate_inner_thought(
        settings, api_client=None, turn_tier="standard"
    )
    assert not should_generate_inner_thought(
        InnerThoughtSettings(enabled=False),
        api_client=object(),  # type: ignore[arg-type]
        turn_tier="standard",
    )
    assert not should_generate_inner_thought(
        settings,
        api_client=object(),  # type: ignore[arg-type]
        turn_tier="fast",
    )
    assert should_generate_inner_thought(
        settings,
        api_client=object(),  # type: ignore[arg-type]
        turn_tier="standard",
    )


def test_format_recent_dialogue_keeps_tail() -> None:
    messages = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "こんにちは"},
        {"role": "user", "content": "在吗"},
        {"role": "assistant", "content": "いるよ"},
        {"role": "user", "content": "第三句"},
    ]
    text = format_recent_dialogue(messages, max_turns=2)
    assert "第三句" in text
    assert "在吗" in text
    assert "你好" not in text


def test_window_labels_align_old_to_new() -> None:
    assert _window_labels(1) == ["今"]
    assert _window_labels(3) == ["前々", "前", "今"]
