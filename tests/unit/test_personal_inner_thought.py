"""Inner-thought grammar, private fragment, and one-call transport."""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.inner_thought import (
    DEFAULT_INNER_THOUGHT_MAX_TOKENS,
    DEFAULT_INNER_THOUGHT_WINDOW_SIZE,
    InnerThoughtSettings,
    InnerThoughtWindow,
    build_inner_thought_fragment,
    build_inner_thought_system_prompt,
    build_inner_thought_user_prompt,
    format_recent_dialogue,
    generate_inner_thought,
    parse_inner_thought_output,
    should_generate_inner_thought,
)
from app.llm.api_client import ApiRequestError, ApiSettings, OpenAICompatibleClient


def test_parser_keeps_optional_three_line_appraisal_and_drops_defects() -> None:
    parsed = parse_inner_thought_output(
        "interest: mid\n"
        "drive_kind: attachment_longing\n"
        "drive_shift: rise\n"
        "drive_strength: mild\n"
        "雨なら傘を持てばいい。"
    )
    assert parsed.interest == "mid"
    assert parsed.text == "雨なら傘を持てばいい。"
    assert parsed.drive_appraisal is not None
    assert parsed.drive_appraisal.kind == "attachment_longing"
    assert parsed.drive_appraisal.direction == "rise"
    assert parsed.drive_appraisal.strength == "mild"

    partial = parse_inner_thought_output(
        "interest: 高\ndrive_kind: afterglow\nまだ何も言わない。"
    )
    assert partial.interest == "high"
    assert partial.text == "まだ何も言わない。"
    assert partial.drive_appraisal is None

    unknown = parse_inner_thought_output(
        "interest: low\ndrive_note: secret\n静か。"
    )
    assert unknown.drive_appraisal is None
    assert unknown.text == "静か。"

    strong = parse_inner_thought_output(
        "interest: low\n"
        "drive_kind: inhibition\n"
        "drive_shift: hold\n"
        "drive_strength: strong\n"
        "足りない。"
    )
    assert strong.drive_appraisal is None
    assert strong.text == "足りない。"


def test_window_fragment_and_prompt_keep_private_bounds() -> None:
    window = InnerThoughtWindow(DEFAULT_INNER_THOUGHT_WINDOW_SIZE)
    for index in range(7):
        window.push(f"thought-{index}")
    assert len(window) == 6
    assert window.items()[0] == "thought-1"
    assert window.items()[-1] == "thought-6"

    fragment = build_inner_thought_fragment(window, character_name="Sakura")
    assert fragment is not None
    assert fragment.fragment_id == "runtime.inner_thought"
    assert fragment.sensitivity == "private"
    assert fragment.token_budget == 420
    assert fragment.priority == 88
    assert "thought-0" not in fragment.content
    assert "内心の声" in fragment.content

    system_prompt = build_inner_thought_system_prompt("Sakura")
    assert "interest: low|mid|high" in system_prompt
    assert "drive_kind:" in system_prompt
    user_prompt = build_inner_thought_user_prompt(
        character_name="Sakura",
        character_excerpt="excerpt",
        mood_summary="",
        recent_dialogue="用户：在吗",
        sensory_impression="",
        previous_thoughts=window.items(),
    )
    assert "最近感知印象" not in user_prompt
    assert "interest: low|mid|high" in user_prompt
    dialogue = format_recent_dialogue(
        [
            {"role": "user", "content": "在吗"},
            {"role": "assistant", "content": "うん", "source": "proactive"},
            {"role": "system", "content": "skip"},
        ]
    )
    assert "用户：在吗" in dialogue
    assert "角色(主动)：うん" in dialogue
    assert "skip" not in dialogue


def test_generation_uses_supported_params_and_skips_without_client() -> None:
    seen: dict[str, Any] = {}

    class Client:
        def complete_raw(self, system_prompt: str, messages: list[dict[str, str]], **kwargs: Any) -> str:
            seen["system"] = system_prompt
            seen["kwargs"] = kwargs
            return "interest: low\n静か。"

    result = generate_inner_thought(
        Client(),
        character_name="Sakura",
        character_excerpt="card",
        mood_summary="",
        recent_dialogue="用户：你好",
        previous_thoughts=(),
    )
    assert result.text == "静か。"
    assert seen["kwargs"]["max_tokens"] == DEFAULT_INNER_THOUGHT_MAX_TOKENS
    assert set(seen["kwargs"]) <= {"temperature", "max_tokens", "cancel_checker"}
    assert should_generate_inner_thought(
        InnerThoughtSettings(enabled=False),
        api_client=Client(),
    ) is False
    assert should_generate_inner_thought(
        InnerThoughtSettings(),
        api_client=None,
    ) is False
    assert should_generate_inner_thought(
        InnerThoughtSettings(),
        api_client=Client(),
        turn_tier="fast",
    ) is False


def test_dedicated_client_uses_one_attempt_and_kwargs_do_not_limit_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    client = OpenAICompatibleClient(
        ApiSettings(base_url="https://fixture.invalid/v1", api_key="key", model="thought", timeout_seconds=8),
        request_attempts=1,
    )

    def fake_post(payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        calls.append(payload)
        raise ApiRequestError("temperature only supports the default value")

    monkeypatch.setattr(client, "_post_chat_completions", fake_post)
    with pytest.raises(ApiRequestError):
        client.complete_raw(
            "system",
            [{"role": "user", "content": "hi"}],
            temperature=0.9,
            max_tokens=180,
        )
    assert len(calls) == 1
    assert client.settings.timeout_seconds == 8

    loose = OpenAICompatibleClient(
        ApiSettings(base_url="https://fixture.invalid/v1", api_key="key", model="chat", timeout_seconds=60),
    )
    loose_calls: list[int] = []

    def loose_post(payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        loose_calls.append(1)
        if "temperature" in payload:
            raise ApiRequestError("temperature only supports the default value")
        raise ApiRequestError("still broken")

    monkeypatch.setattr(loose, "_post_chat_completions", loose_post)
    with pytest.raises(ApiRequestError):
        loose.complete_raw(
            "system",
            [{"role": "user", "content": "hi"}],
            temperature=0.9,
            request_timeout=1,
            max_attempts=1,
        )
    assert len(loose_calls) > 1
    assert loose.settings.timeout_seconds == 60

    # Existing retry_requests=False disables network retries, not protocol adaptation.
    legacy = OpenAICompatibleClient(loose.settings, retry_requests=False)
    legacy_calls = []

    def legacy_post(payload, **kwargs):
        legacy_calls.append(payload)
        if "temperature" in payload:
            raise ApiRequestError("temperature only supports the default value")
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(legacy, "_post_chat_completions", legacy_post)
    assert legacy.complete_raw("system", [], temperature=0.9) == "ok"
    assert len(legacy_calls) == 2
