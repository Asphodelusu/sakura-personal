"""P0：请求分区、purpose、稳定前缀与 usage 白名单。默认结果不得含正文。"""

from __future__ import annotations

import json
from typing import Any

from app.core.interaction import set_interaction_id
from app.llm.api_client import ApiSettings, OpenAICompatibleClient
from app.llm.payload_inspection import (
    canonical_json_bytes,
    extract_usage_metrics,
    inspect_chat_payload,
    inspection_log_dict,
    normalize_request_purpose,
    payload_sha256,
)


SECRET_PROMPT = "PERSONA-SECRET-NEVER-LOG"
SECRET_TOOL = "tool-result-secret-never-log"
SECRET_KEY = "sk-synthetic-never-log"
DATA_URL = "data:image/png;base64,AAAABBBBCCCC"

SHORT_SYSTEM = "synthetic-system-role"
SHORT_USER = "synthetic-user-hi"
SHORT_ASSISTANT = "synthetic-assistant-hi"
RUNTIME_FACT = "synthetic-runtime-clock=00:00"


def _payload(*, extra_messages: list[dict[str, Any]] | None = None, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    messages = [
        {"role": "system", "content": SHORT_SYSTEM},
        {"role": "user", "content": SHORT_USER},
        {"role": "assistant", "content": SHORT_ASSISTANT},
        {"role": "system", "content": RUNTIME_FACT},
    ]
    if extra_messages:
        messages[1:1] = extra_messages
    body: dict[str, Any] = {
        "model": "synthetic-model",
        "temperature": 0.8,
        "messages": messages,
    }
    if tools is not None:
        body["tools"] = tools
    return body


def test_canonical_json_hash_is_stable_across_key_order() -> None:
    left = {"b": 2, "a": {"z": 1, "m": 0}}
    right = {"a": {"m": 0, "z": 1}, "b": 2}
    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert payload_sha256(left) == payload_sha256(right)


def test_canonical_json_hash_changes_when_content_changes() -> None:
    assert payload_sha256({"a": 1}) != payload_sha256({"a": 2})


def test_unknown_purpose_is_explicit_not_guessed() -> None:
    assert normalize_request_purpose(None) == "unknown"
    assert normalize_request_purpose("") == "unknown"
    assert normalize_request_purpose("compose") == "unknown"
    assert normalize_request_purpose("initial") == "initial"
    assert normalize_request_purpose("tool_step") == "tool_step"
    assert normalize_request_purpose("semantic_compose") == "semantic_compose"
    assert normalize_request_purpose("structural_repair") == "structural_repair"


def test_inspect_partitions_are_accountable_and_bodyless() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "memory_search",
                "description": SECRET_TOOL,
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    inspection = inspect_chat_payload(
        _payload(tools=tools),
        runtime_context=RUNTIME_FACT,
        interaction_id="interaction-1",
        request_index=1,
        request_purpose="initial",
        endpoint="https://api.example.test/v1",
        model="synthetic-model",
    )
    dumped = json.dumps(inspection_log_dict(inspection), ensure_ascii=False)
    for secret in (SECRET_PROMPT, SECRET_TOOL, SHORT_SYSTEM, SHORT_USER, SHORT_ASSISTANT, RUNTIME_FACT, DATA_URL):
        assert secret not in dumped

    for name in ("system", "messages", "runtime", "tools", "image", "whole"):
        part = inspection.partitions[name]
        assert part is not None
        assert part.bytes > 0 or name == "image"
        assert part.estimated_tokens >= 0
        assert len(part.hash) == 64

    assert inspection.partitions["image"].bytes == 0
    assert inspection.partitions["image"].estimated_tokens == 0
    assert inspection.partitions["system"].bytes != inspection.partitions["messages"].bytes
    assert inspection.partitions["runtime"].bytes > 0
    assert inspection.interaction_id == "interaction-1"
    assert inspection.request_index == 1
    assert inspection.request_purpose == "initial"


def test_runtime_partition_is_null_when_not_unambiguous() -> None:
    payload = {
        "model": "synthetic-model",
        "messages": [
            {"role": "system", "content": SHORT_SYSTEM},
            {"role": "user", "content": SHORT_USER + RUNTIME_FACT},
        ],
    }
    inspection = inspect_chat_payload(
        payload,
        runtime_context=RUNTIME_FACT,
        request_purpose="initial",
    )
    assert inspection.partitions["runtime"] is None


def test_image_estimate_hashes_count_not_data_url() -> None:
    payload = {
        "model": "synthetic-model",
        "messages": [
            {"role": "system", "content": SHORT_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": SHORT_USER},
                    {"type": "image_url", "image_url": {"url": DATA_URL}},
                ],
            },
        ],
    }
    inspection = inspect_chat_payload(payload, request_purpose="initial")
    image = inspection.partitions["image"]
    assert image is not None
    assert image.estimated_tokens == 28_000
    assert image.bytes > 0
    log = json.dumps(inspection_log_dict(inspection), ensure_ascii=False)
    assert DATA_URL not in log
    assert "AAAABBBBCCCC" not in log


def test_usage_whitelist_reads_cached_and_reasoning_without_guessing() -> None:
    deepseek = extract_usage_metrics(
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 40},
            "completion_tokens_details": {"reasoning_tokens": 8},
            "prompt_cache_hit_tokens": 40,
        }
    )
    assert deepseek == {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "cached_input_tokens": 40,
        "reasoning_tokens": 8,
    }

    gemini = extract_usage_metrics(
        {
            "prompt_tokens": 80,
            "completion_tokens": 10,
            "total_tokens": 90,
        }
    )
    assert gemini["cached_input_tokens"] is None
    assert gemini["reasoning_tokens"] is None
    assert gemini["input_tokens"] == 80


def test_stable_prefix_compares_adjacent_same_endpoint_model_purpose() -> None:
    first_payload = _payload()
    first = inspect_chat_payload(
        first_payload,
        runtime_context=RUNTIME_FACT,
        request_purpose="initial",
        endpoint="https://api.example.test/v1",
        model="synthetic-model",
    )
    second_payload = _payload()
    second_payload["messages"][1] = {"role": "user", "content": "synthetic-user-hi-2"}
    second = inspect_chat_payload(
        second_payload,
        runtime_context=RUNTIME_FACT,
        request_purpose="initial",
        endpoint="https://api.example.test/v1",
        model="synthetic-model",
        previous=first,
        previous_payload=first_payload,
    )
    assert second.stable_prefix_bytes is not None
    assert second.stable_prefix_bytes > 0
    assert second.stable_prefix_bytes < second.partitions["whole"].bytes
    assert second.stable_prefix_hash
    other_purpose = inspect_chat_payload(
        second_payload,
        runtime_context=RUNTIME_FACT,
        request_purpose="tool_step",
        endpoint="https://api.example.test/v1",
        model="synthetic-model",
        previous=first,
        previous_payload=first_payload,
    )
    assert other_purpose.stable_prefix_bytes is None
    assert other_purpose.stable_prefix_hash is None


def test_inspection_does_not_retain_hidden_payload_body() -> None:
    inspection = inspect_chat_payload(
        _payload(),
        runtime_context=RUNTIME_FACT,
        request_purpose="initial",
    )

    assert not hasattr(inspection, "_whole_bytes")


def test_deepseek_prompt_cache_hit_tokens_are_whitelisted() -> None:
    metrics = extract_usage_metrics(
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_cache_hit_tokens": 73,
            "private_provider_field": "never-forward",
        }
    )

    assert metrics["cached_input_tokens"] == 73
    assert "private_provider_field" not in metrics


def test_complete_with_tools_records_purpose_without_changing_provider_payload(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: dict[str, Any] = {}

    def fake_post(payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        captured["payload"] = dict(payload)
        return {
            "choices": [{"message": {"content": '{"segments":[]}', "role": "assistant"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 3,
                "total_tokens": 14,
                "prompt_tokens_details": {"cached_tokens": 2},
                "secret_field": "LEAK",
            },
        }

    client = OpenAICompatibleClient(
        ApiSettings(
            api_key=SECRET_KEY,
            base_url="https://api.deepseek.com/v1",
            model="deepseek-chat",
        )
    )
    monkeypatch.setattr(client, "_post_chat_completions_with_compatibility_fallbacks", fake_post)
    set_interaction_id("interaction-measure")
    try:
        client.complete_with_tools(
            SECRET_PROMPT,
            [{"role": "user", "content": SHORT_USER}],
            tools=[],
            request_purpose="initial",
        )
        client.complete_with_tools(
            SECRET_PROMPT,
            [{"role": "user", "content": SHORT_USER}],
            tools=[],
            request_purpose="semantic_compose",
        )
    finally:
        set_interaction_id("")

    sent = captured["payload"]
    assert "request_purpose" not in sent
    assert "interaction_id" not in sent
    assert "request_index" not in json.dumps(sent)
    assert sent["messages"][0]["content"] == SECRET_PROMPT
    assert sent["model"] == "deepseek-chat"

    first, second = client.payload_inspections
    assert first.request_purpose == "initial"
    assert second.request_purpose == "semantic_compose"
    assert first.interaction_id == "interaction-measure"
    assert second.interaction_id == "interaction-measure"
    assert first.request_index == 1
    assert second.request_index == 2
    assert first.usage["cached_input_tokens"] == 2
    assert "LEAK" not in json.dumps(inspection_log_dict(first))
    assert SECRET_KEY not in json.dumps(inspection_log_dict(first))
    assert SECRET_PROMPT not in json.dumps(inspection_log_dict(first))


def test_complete_raw_records_unknown_or_explicit_purpose_without_changing_payload(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: list[dict[str, Any]] = []

    def fake_post(payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        captured.append(dict(payload))
        return {
            "choices": [{"message": {"content": "synthetic-ok", "role": "assistant"}}],
            "usage": {
                "prompt_tokens": 9,
                "completion_tokens": 2,
                "total_tokens": 11,
                "prompt_cache_hit_tokens": 4,
            },
        }

    client = OpenAICompatibleClient(
        ApiSettings(
            api_key=SECRET_KEY,
            base_url="https://api.deepseek.com/v1",
            model="deepseek-chat",
        )
    )
    monkeypatch.setattr(client, "_post_chat_completions_with_compatibility_fallbacks", fake_post)

    client.complete_raw(SECRET_PROMPT, [{"role": "user", "content": SHORT_USER}])
    client.complete_raw(
        SECRET_PROMPT,
        [{"role": "user", "content": SHORT_USER}],
        request_purpose="initial",
    )

    assert [item.request_purpose for item in client.payload_inspections] == ["unknown", "initial"]
    assert client.payload_inspections[-1].usage["cached_input_tokens"] == 4
    assert all("request_purpose" not in payload for payload in captured)
    assert all("request_index" not in payload for payload in captured)


def test_client_measurement_state_is_bounded(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_post(_payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        return {"choices": [{"message": {"content": "ok", "role": "assistant"}}]}

    client = OpenAICompatibleClient(
        ApiSettings(api_key=SECRET_KEY, base_url="https://api.deepseek.com/v1", model="deepseek-chat")
    )
    monkeypatch.setattr(client, "_post_chat_completions_with_compatibility_fallbacks", fake_post)

    for index in range(300):
        client.complete_raw(
            SECRET_PROMPT,
            [{"role": "user", "content": SHORT_USER}],
            interaction_id=f"interaction-{index}",
        )

    assert len(client.payload_inspections) <= 128
    assert len(client._request_index_by_interaction) <= 128
