from __future__ import annotations

import copy
import json
from typing import Any, Iterator

import httpx
import pytest

from app.llm import api_client
from app.llm.prompts.runtime import (
    RUNTIME_FACTS_SLOT_MARKER,
    RUNTIME_NOW_SLOT_MARKER,
)


SLOTTED_RUNTIME_CONTEXT = (
    f"{RUNTIME_FACTS_SLOT_MARKER}\nfixture facts\n"
    f"{RUNTIME_NOW_SLOT_MARKER}\nfixture now"
)


def _settings(
    *,
    base_url: str = "https://api.example.com/v1/",
    model: str = "text-model",
    text_model: str = "",
    model_split_enabled: bool = False,
) -> api_client.ApiSettings:
    return api_client.ApiSettings(
        base_url=base_url,
        api_key="fixture-key",
        model=model,
        text_model=text_model,
        model_split_enabled=model_split_enabled,
    )


def _completion(content: str = "OK") -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _role_error(
    message: str,
    *,
    status_code: int = 400,
    param: str | None = None,
) -> api_client.ApiRequestError:
    return api_client.ApiRequestError(
        f"API HTTP {status_code}: fixture rejection",
        status_code=status_code,
        error_message=message,
        error_code="invalid_request_error",
        error_param=param,
    )


def _message_index(payload: dict[str, Any], marker: str) -> int:
    return next(
        index
        for index, message in enumerate(payload["messages"])
        if marker in str(message.get("content") or "")
    )


def test_runtime_context_system_indices_match_only_exact_wire_slots() -> None:
    messages = [
        {"role": "system", "content": "base"},
        {"role": "system", "content": f"{RUNTIME_FACTS_SLOT_MARKER}\nunrelated"},
        {"role": "system", "content": f"{RUNTIME_FACTS_SLOT_MARKER}\nfixture facts"},
        {"role": "user", "content": "hello"},
        {"role": "system", "content": f"{RUNTIME_NOW_SLOT_MARKER}\nfixture now"},
    ]

    assert api_client._runtime_context_system_message_indices(
        messages,
        SLOTTED_RUNTIME_CONTEXT,
    ) == frozenset({2, 4})


@pytest.mark.parametrize(
    ("message", "status_code"),
    [
        ("invalid messages[2].content", 400),
        ("tool message has invalid order", 400),
        ("invalid image_url in messages[1].content", 422),
        ("system overloaded; invalid upstream response", 500),
        ("invalid role", 400),
        ("system unsupported", 400),
    ],
)
def test_classifier_does_not_treat_unrelated_errors_as_system_position_rejections(
    message: str,
    status_code: int,
) -> None:
    rejection = api_client._classify_runtime_context_system_rejection(
        _role_error(message, status_code=status_code),
        runtime_message_index=2,
    )

    assert rejection == api_client.RuntimeContextSystemRejection.NONE


@pytest.mark.parametrize("status_code", [400, 422])
def test_classifier_accepts_only_validation_statuses_for_mid_system_rejection(
    status_code: int,
) -> None:
    rejection = api_client._classify_runtime_context_system_rejection(
        _role_error(
            "system message is not allowed in the middle of messages",
            status_code=status_code,
            param="messages[2].role",
        ),
        runtime_message_index=2,
    )

    assert rejection == api_client.RuntimeContextSystemRejection.MID_SYSTEM_REJECTED


@pytest.mark.parametrize("status_code", [401, 403, 429, 500, 503])
def test_classifier_rejects_non_validation_statuses(status_code: int) -> None:
    rejection = api_client._classify_runtime_context_system_rejection(
        _role_error(
            "system message is not allowed in the middle of messages",
            status_code=status_code,
            param="messages[2].role",
        ),
        runtime_message_index=2,
    )

    assert rejection == api_client.RuntimeContextSystemRejection.NONE


def test_classifier_prioritizes_noninitial_system_rejection() -> None:
    rejection = api_client._classify_runtime_context_system_rejection(
        _role_error(
            "Only one system message is allowed; system messages must be first, "
            "not in the middle of messages",
            param="messages[2].role",
        ),
        runtime_message_index=2,
    )

    assert rejection == api_client.RuntimeContextSystemRejection.NONINITIAL_SYSTEM_REJECTED


def test_classifier_ignores_error_index_for_a_different_message() -> None:
    rejection = api_client._classify_runtime_context_system_rejection(
        _role_error(
            "system message must be first",
            param="messages[0].role",
        ),
        runtime_message_index=2,
    )

    assert rejection == api_client.RuntimeContextSystemRejection.NONE


def test_http_error_preserves_display_text_and_structured_metadata() -> None:
    body = json.dumps(
        {
            "error": {
                "message": "system message must be first",
                "code": "invalid_message_role",
                "param": "messages[2].role",
            }
        }
    )

    exc = api_client._api_request_error(422, body, "https://api.example.com/v1")

    assert str(exc) == f"API HTTP 422: {body}"
    assert exc.status_code == 422
    assert exc.error_message == "system message must be first"
    assert exc.error_code == "invalid_message_role"
    assert exc.error_param == "messages[2].role"


def test_http_transport_attaches_structured_error_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps(
        {
            "error": {
                "message": "system messages must be first",
                "code": "invalid_message_role",
                "param": "messages[2].role",
            }
        }
    )
    response = type(
        "_Response",
        (),
        {
            "text": body,
            "status_code": 422,
            "url": "https://api.example.com/v1/models",
        },
    )()
    transport = type("_Transport", (), {"request": lambda *_args, **_kwargs: response})()
    client = api_client.OpenAICompatibleClient(_settings())
    monkeypatch.setattr(client, "_http_client", lambda: transport)

    with pytest.raises(api_client.ApiRequestError) as raised:
        client.list_models()

    assert raised.value.status_code == 422
    assert raised.value.error_message == "system messages must be first"
    assert raised.value.error_code == "invalid_message_role"
    assert raised.value.error_param == "messages[2].role"


def test_complete_raw_falls_back_once_and_remembers_capability_per_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = api_client.OpenAICompatibleClient(_settings())
    calls: list[dict[str, Any]] = []

    def fake_post(payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        calls.append(copy.deepcopy(payload))
        if len(calls) == 1:
            raise _role_error(
                "system message is not allowed in the middle of messages",
                param=f"messages[{len(payload['messages']) - 1}].role",
            )
        return _completion()

    monkeypatch.setattr(client, "_post_chat_completions_with_compatibility_fallbacks", fake_post)
    original = [{"role": "user", "content": "hello"}]

    assert client.complete_raw("system", original, runtime_context="facts") == "OK"
    assert client.complete_raw("system", original, runtime_context="facts") == "OK"

    assert [payload["messages"][-1]["role"] for payload in calls] == ["system", "user", "user"]
    assert calls[1]["messages"][-1]["content"].startswith("[Sakura runtime context")
    assert original == [{"role": "user", "content": "hello"}]
    capability = client.runtime_context_capability(original)
    assert capability.trailing_system_ok is True
    assert capability.mid_array_system_ok is False
    assert capability.successful_user_fallback_calls == 2
    assert client.runtime_context_role == "user"


def test_complete_raw_matches_indexed_l5_rejection_in_slotted_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = api_client.OpenAICompatibleClient(_settings())
    calls: list[dict[str, Any]] = []

    def fake_post(payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        calls.append(copy.deepcopy(payload))
        if len(calls) == 1:
            l5_index = _message_index(payload, RUNTIME_FACTS_SLOT_MARKER)
            assert l5_index != len(payload["messages"]) - 1
            raise _role_error(
                "system message is not allowed in the middle of messages",
                param=f"messages[{l5_index}].role",
            )
        return _completion()

    monkeypatch.setattr(client, "_post_chat_completions_with_compatibility_fallbacks", fake_post)

    assert client.complete_raw(
        "system",
        [{"role": "user", "content": "hello"}],
        runtime_context=SLOTTED_RUNTIME_CONTEXT,
    ) == "OK"
    assert [
        payload["messages"][_message_index(payload, RUNTIME_FACTS_SLOT_MARKER)]["role"]
        for payload in calls
    ] == ["system", "user"]


def test_complete_raw_keeps_downgrade_when_user_fallback_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = api_client.OpenAICompatibleClient(_settings())
    calls: list[dict[str, Any]] = []

    def fake_post(payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        calls.append(copy.deepcopy(payload))
        if len(calls) == 1:
            raise _role_error("system messages must be first")
        raise api_client.ApiRequestError("fallback failed", status_code=500)

    monkeypatch.setattr(client, "_post_chat_completions_with_compatibility_fallbacks", fake_post)

    with pytest.raises(api_client.ApiRequestError, match="fallback failed"):
        client.complete_raw("system", [{"role": "user", "content": "hello"}], runtime_context="facts")

    capability = client.runtime_context_capability([{"role": "user", "content": "hello"}])
    assert [payload["messages"][-1]["role"] for payload in calls] == ["system", "user"]
    assert capability.trailing_system_ok is False
    assert capability.mid_array_system_ok is False
    assert capability.successful_user_fallback_calls == 0


def test_complete_raw_without_runtime_context_never_role_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = api_client.OpenAICompatibleClient(_settings())
    calls = 0

    def fake_post(_payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise _role_error("system messages must be first")

    monkeypatch.setattr(client, "_post_chat_completions_with_compatibility_fallbacks", fake_post)

    with pytest.raises(api_client.ApiRequestError):
        client.complete_raw("system", [{"role": "user", "content": "hello"}])

    assert calls == 1
    assert client.runtime_context_capability([]).fallback_tier == 0


def test_runtime_capability_isolated_between_text_and_vision_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = api_client.OpenAICompatibleClient(
        _settings(model="vision-model", text_model="text-model", model_split_enabled=True)
    )
    calls: list[dict[str, Any]] = []

    def fake_post(payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        calls.append(copy.deepcopy(payload))
        if payload["model"] == "text-model" and payload["messages"][-1]["role"] == "system":
            raise _role_error("system message is not allowed in the middle of messages")
        return _completion()

    monkeypatch.setattr(client, "_post_chat_completions_with_compatibility_fallbacks", fake_post)
    text_messages = [{"role": "user", "content": "hello"}]
    image_messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
            ],
        }
    ]

    client.complete_raw("system", text_messages, runtime_context="facts")
    client.complete_raw("system", image_messages, runtime_context="facts")

    assert [(payload["model"], payload["messages"][-1]["role"]) for payload in calls] == [
        ("text-model", "system"),
        ("text-model", "user"),
        ("vision-model", "system"),
    ]
    assert client.runtime_context_capability(text_messages).mid_array_system_ok is False
    assert client.runtime_context_capability(image_messages).mid_array_system_ok is True


def test_runtime_capability_key_preserves_endpoint_path_and_model() -> None:
    client = api_client.OpenAICompatibleClient(_settings())

    first = client.runtime_context_capability([], endpoint="https://API.example.com/v1/")
    second = client.runtime_context_capability([], endpoint="https://api.example.com/proxy/v1")
    other_model = client.runtime_context_capability(
        [{"role": "user", "content": "hello"}],
        endpoint="https://api.example.com/v1",
        model="other-model",
    )

    assert first.key == ("https://api.example.com/v1", "text-model")
    assert second.key == ("https://api.example.com/proxy/v1", "text-model")
    assert other_model.key == ("https://api.example.com/v1", "other-model")


def test_update_settings_discards_stale_request_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = api_client.OpenAICompatibleClient(_settings(model="old-model"))
    calls = 0

    def fake_post(_payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        client.update_settings(_settings(model="new-model"))
        raise _role_error("system messages must be first")

    monkeypatch.setattr(client, "_post_chat_completions_with_compatibility_fallbacks", fake_post)

    with pytest.raises(api_client.ApiRequestError):
        client.complete_raw(
            "system",
            [{"role": "user", "content": "hello"}],
            runtime_context="facts",
        )

    assert calls == 1
    assert client.runtime_context_capability([]).fallback_tier == 0
    assert client.runtime_context_role == "system"


def test_complete_with_tools_uses_shared_fallback_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = api_client.OpenAICompatibleClient(_settings())
    roles: list[str] = []

    def fake_post(payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        roles.append(payload["messages"][-1]["role"])
        if len(roles) == 1:
            raise _role_error("system messages must be first")
        return _completion()

    monkeypatch.setattr(client, "_post_chat_completions_with_compatibility_fallbacks", fake_post)

    turn = client.complete_with_tools(
        "system",
        [{"role": "user", "content": "hello"}],
        tools=[],
        runtime_context="facts",
    )

    assert roles == ["system", "user"]
    assert turn.runtime_context_role == "user"
    assert client.runtime_context_capability([]).successful_user_fallback_calls == 1


def test_complete_with_tools_matches_indexed_l5_rejection_in_slotted_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = api_client.OpenAICompatibleClient(_settings())
    calls: list[dict[str, Any]] = []

    def fake_post(payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        calls.append(copy.deepcopy(payload))
        if len(calls) == 1:
            l5_index = _message_index(payload, RUNTIME_FACTS_SLOT_MARKER)
            raise _role_error(
                "system message is not allowed in the middle of messages",
                param=f"messages[{l5_index}].role",
            )
        return _completion()

    monkeypatch.setattr(client, "_post_chat_completions_with_compatibility_fallbacks", fake_post)

    turn = client.complete_with_tools(
        "system",
        [{"role": "user", "content": "hello"}],
        tools=[],
        runtime_context=SLOTTED_RUNTIME_CONTEXT,
    )

    assert turn.content == "OK"
    assert [
        payload["messages"][_message_index(payload, RUNTIME_FACTS_SLOT_MARKER)]["role"]
        for payload in calls
    ] == ["system", "user"]


class _StreamResponse:
    def __init__(
        self,
        status_code: int,
        *,
        body: str = "",
        lines: list[str] | None = None,
        stream_error: BaseException | None = None,
    ) -> None:
        self.status_code = status_code
        self.url = "https://api.example.com/v1/chat/completions"
        self._body = body
        self._lines = lines or []
        self._stream_error = stream_error

    def read(self) -> bytes:
        return self._body.encode("utf-8")

    def iter_lines(self) -> Iterator[str]:
        yield from self._lines
        if self._stream_error is not None:
            raise self._stream_error


class _StreamContext:
    def __init__(self, response: _StreamResponse) -> None:
        self._response = response

    def __enter__(self) -> _StreamResponse:
        return self._response

    def __exit__(self, *_args: Any) -> None:
        return None


class _StreamClient:
    def __init__(self, responses: list[_StreamResponse]) -> None:
        self._responses = list(responses)
        self.payloads: list[dict[str, Any]] = []

    def stream(self, _method: str, _path: str, **kwargs: Any) -> _StreamContext:
        self.payloads.append(json.loads(kwargs["content"]))
        return _StreamContext(self._responses.pop(0))


def test_stream_raw_retries_only_a_handshake_role_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejection_body = json.dumps(
        {
            "error": {
                "message": "system messages must be first",
                "code": "invalid_message_role",
                "param": "messages[2].role",
            }
        }
    )
    transport = _StreamClient(
        [
            _StreamResponse(400, body=rejection_body),
            _StreamResponse(
                200,
                lines=[
                    'data: {"choices":[{"delta":{"content":"OK"}}]}',
                    "data: [DONE]",
                ],
            ),
        ]
    )
    client = api_client.OpenAICompatibleClient(_settings())
    monkeypatch.setattr(client, "_http_client", lambda: transport)

    chunks = list(
        client.stream_raw(
            "system",
            [{"role": "user", "content": "hello"}],
            runtime_context="facts",
        )
    )

    assert chunks == ["OK"]
    assert [payload["messages"][-1]["role"] for payload in transport.payloads] == [
        "system",
        "user",
    ]
    assert client.runtime_context_capability([]).successful_user_fallback_calls == 1


def test_stream_raw_matches_indexed_l5_rejection_in_slotted_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejection_body = json.dumps(
        {
            "error": {
                "message": "system message is not allowed in the middle of messages",
                "code": "invalid_message_role",
                "param": "messages[1].role",
            }
        }
    )
    transport = _StreamClient(
        [
            _StreamResponse(400, body=rejection_body),
            _StreamResponse(
                200,
                lines=[
                    'data: {"choices":[{"delta":{"content":"OK"}}]}',
                    "data: [DONE]",
                ],
            ),
        ]
    )
    client = api_client.OpenAICompatibleClient(_settings())
    monkeypatch.setattr(client, "_http_client", lambda: transport)

    chunks = list(
        client.stream_raw(
            "system",
            [{"role": "user", "content": "hello"}],
            runtime_context=SLOTTED_RUNTIME_CONTEXT,
        )
    )

    assert chunks == ["OK"]
    assert [
        payload["messages"][_message_index(payload, RUNTIME_FACTS_SLOT_MARKER)]["role"]
        for payload in transport.payloads
    ] == ["system", "user"]


def test_stream_raw_stops_when_same_rejection_cannot_advance_fallback_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejection_body = json.dumps(
        {
            "error": {
                "message": "system message is not allowed in the middle of messages",
                "code": "invalid_message_role",
            }
        }
    )
    transport = _StreamClient([_StreamResponse(400, body=rejection_body)])
    client = api_client.OpenAICompatibleClient(_settings())
    capability = client.runtime_context_capability([])
    assert client._downgrade_runtime_context_capability(
        key=capability.key,
        generation=0,
        rejection=api_client.RuntimeContextSystemRejection.MID_SYSTEM_REJECTED,
    )
    monkeypatch.setattr(client, "_http_client", lambda: transport)

    with pytest.raises(api_client.ApiRequestError, match="API HTTP 400"):
        list(
            client.stream_raw(
                "system",
                [{"role": "user", "content": "hello"}],
                runtime_context=SLOTTED_RUNTIME_CONTEXT,
            )
        )

    assert len(transport.payloads) == 1


def test_stream_raw_never_replays_after_response_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _StreamClient(
        [
            _StreamResponse(
                200,
                lines=['data: {"choices":[{"delta":{"content":"first"}}]}'],
                stream_error=_role_error("system messages must be first"),
            )
        ]
    )
    client = api_client.OpenAICompatibleClient(_settings())
    monkeypatch.setattr(client, "_http_client", lambda: transport)

    iterator = client.stream_raw(
        "system",
        [{"role": "user", "content": "hello"}],
        runtime_context="facts",
    )
    assert next(iterator) == "first"
    with pytest.raises(api_client.ApiRequestError):
        next(iterator)

    assert len(transport.payloads) == 1
    assert client.runtime_context_capability([]).fallback_tier == 0


def test_parameter_and_role_fallbacks_have_a_bounded_combined_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = api_client.OpenAICompatibleClient(_settings())
    calls: list[dict[str, Any]] = []

    def fake_post(payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        calls.append(copy.deepcopy(payload))
        if "response_format" in payload:
            raise api_client.ApiRequestError("unsupported response_format json_object")
        if payload["messages"][-1]["role"] == "system":
            raise _role_error("system messages must be first")
        return _completion()

    monkeypatch.setattr(client, "_post_chat_completions", fake_post)

    assert (
        client.complete_raw(
            "system",
            [{"role": "user", "content": "hello"}],
            runtime_context="facts",
            response_format={"type": "json_object"},
        )
        == "OK"
    )

    assert len(calls) == 3
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]
    assert calls[1]["messages"][-1]["role"] == "system"
    assert calls[2]["messages"][-1]["role"] == "user"
