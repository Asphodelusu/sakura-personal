from __future__ import annotations

from typing import Any

from app.llm.json_completion import complete_background_json, load_json_object


def test_load_json_object_extracts_embedded_object() -> None:
    raw = '我们被要求输出JSON...\n{"reflections":[{"content":"关系更近了","importance":0.7,"confidence":0.8}]}'
    data = load_json_object(raw)
    assert data["reflections"][0]["content"] == "关系更近了"


def test_complete_background_json_retries_on_invalid_output() -> None:
    api = _RecordingApiClient(
        "先分析一下……",
        '{"ok": true}',
    )
    parsed, _raw = complete_background_json(
        api,
        "system",
        [{"role": "user", "content": "go"}],
        log_label="Test",
    )
    assert parsed == {"ok": True}
    assert len(api.calls) == 2
    assert api.calls[0]["kwargs"]["thinking"] == {"type": "disabled"}
    assert api.calls[1]["kwargs"]["temperature"] == 0.1


class _RecordingApiClient:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete_raw(self, system_prompt: str, messages: list[dict[str, str]], **kwargs: Any) -> str:
        self.calls.append({"system_prompt": system_prompt, "messages": messages, "kwargs": kwargs})
        if not self.responses:
            return "{}"
        return self.responses.pop(0)
