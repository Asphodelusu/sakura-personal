from __future__ import annotations



import json

from typing import Any



from app.agent.memory import MemoryStore

from app.agent.memory_reflector import (

    MemoryReflector,

    _parse_reflection_output,

)





def test_parse_reflection_output_accepts_code_fence() -> None:

    raw = '```json\n{"reflections":[{"content":"最近对方很忙","importance":0.6,"confidence":0.7}]}\n```'

    reflections = _parse_reflection_output(raw)

    assert len(reflections) == 1

    assert reflections[0]["content"] == "最近对方很忙"





def test_reflector_uses_background_task_and_isolated_prompt() -> None:

    store = _FakeMemoryStore(

        [

            {"id": f"m{i}", "content": f"记忆{i}", "layer": "semantic"}

            for i in range(5)

        ]

    )

    api = _RecordingApiClient(

        '{"reflections":[{"content":"对方最近更依赖我","importance":0.7,"confidence":0.8}]}'

    )

    reflector = MemoryReflector(

        api,

        store,  # type: ignore[arg-type]

        system_prompt="完整 Sakura 人格卡不应出现在反思 prompt 里",

    )



    result = reflector.reflect(memory_store=store)  # type: ignore[arg-type]



    assert result.memories_created == 1

    assert store.created[0]["content"] == "对方最近更依赖我"

    assert api.calls[0]["kwargs"]["task"] == "background"

    assert api.calls[0]["kwargs"]["temperature"] == 0.2

    assert "完整 Sakura 人格卡不应出现在反思 prompt 里" not in api.calls[0]["system_prompt"]

    assert "必须只返回严格 JSON" in api.calls[0]["system_prompt"]





def test_reflector_retries_when_first_response_is_not_json() -> None:

    store = _FakeMemoryStore(

        [

            {"id": f"m{i}", "content": f"记忆{i}", "layer": "semantic"}

            for i in range(5)

        ]

    )

    api = _RecordingApiClient(

        "我们被要求输出JSON，先分析一下……",

        '{"reflections":[{"content":"修复后的反思","importance":0.6,"confidence":0.7}]}',

    )

    reflector = MemoryReflector(api, store)  # type: ignore[arg-type]



    result = reflector.reflect(memory_store=store)  # type: ignore[arg-type]



    assert result.memories_created == 1

    assert len(api.calls) == 2

    assert api.calls[1]["kwargs"]["temperature"] == 0.1

    assert "上一条输出不是合法 JSON" in api.calls[1]["messages"][-1]["content"]





class _FakeMemoryStore:

    def __init__(self, memories: list[dict[str, Any]]) -> None:

        self._memories = memories

        self.created: list[dict[str, Any]] = []



    def list_memories(self, limit: int = 500) -> list[dict[str, Any]]:

        return self._memories[:limit]



    def create_memory(self, payload: dict[str, Any], *, allow_sensitive: bool = False) -> dict[str, Any]:

        self.created.append(payload)

        return {"id": "new", **payload}





class _RecordingApiClient:

    def __init__(self, *responses: str) -> None:

        self.responses = list(responses)

        self.calls: list[dict[str, Any]] = []



    def complete_raw(

        self,

        system_prompt: str,

        messages: list[dict[str, str]],

        **kwargs: Any,

    ) -> str:

        self.calls.append(

            {

                "system_prompt": system_prompt,

                "messages": messages,

                "kwargs": kwargs,

            }

        )

        if not self.responses:

            return '{"reflections":[]}'

        return self.responses.pop(0)

