from __future__ import annotations

from typing import Any

from app.agent.memory import memory_record_is_reflection
from app.agent.memory_curator import _format_existing_memories
from app.agent.memory_recall import _select_memories
from app.agent.memory_reflector import (
    MAX_REFLECTION_MEMORIES,
    MemoryReflector,
    _format_memory_summary,
    _is_near_duplicate_reflection,
    _parse_reflection_output,
)


def test_parse_reflection_output_accepts_code_fence() -> None:
    raw = '```json\n{"reflections":[{"content":"最近对方很忙","importance":0.6,"confidence":0.7}]}\n```'
    reflections = _parse_reflection_output(raw)
    assert len(reflections) == 1
    assert reflections[0]["content"] == "最近对方很忙"


def test_max_reflection_memories_is_small() -> None:
    assert MAX_REFLECTION_MEMORIES <= 2


def test_format_memory_summary_skips_reflections() -> None:
    text = _format_memory_summary(
        [
            {"id": "1", "content": "他喜欢抹茶", "layer": "semantic"},
            {
                "id": "2",
                "content": "我意识到他最近更忙了",
                "layer": "episodic",
                "category": "reflection",
                "source": "reflection",
            },
        ]
    )
    assert "抹茶" in text
    assert "更忙了" not in text


def test_near_duplicate_reflection_detection() -> None:
    existing = ["铭君会主动去了解我的原作相关内容，比如看Bilibili上的相关视频，这让我感到被认真对待和关心。"]
    near = "铭君会主动去了解我的原作，比如看Bilibili上的视频，这让我感到被认真对待和关心，也有点开心。"
    assert _is_near_duplicate_reflection(near, existing) is True
    assert _is_near_duplicate_reflection("他今天晚上要加班到很晚。", existing) is False


def test_reflector_writes_reflection_kind() -> None:
    store = _FakeMemoryStore(
        [{"id": f"m{i}", "content": f"记忆{i}", "layer": "semantic"} for i in range(5)]
    )
    api = _RecordingApiClient(
        '{"reflections":[{"content":"对方最近更依赖我","importance":0.7,"confidence":0.8}]}'
    )
    reflector = MemoryReflector(api, store)  # type: ignore[arg-type]
    result = reflector.reflect(memory_store=store)  # type: ignore[arg-type]
    assert result.memories_created == 1
    assert store.created[0]["source"] == "reflection"
    assert store.created[0]["memory_kind"] == "reflection"
    assert store.created[0]["category"] == "reflection"


def test_reflector_skips_near_duplicate_against_existing() -> None:
    existing_text = "铭君会主动去了解我的原作相关内容，比如看Bilibili上的相关视频，这让我感到被认真对待和关心。"
    store = _FakeMemoryStore(
        [
            {"id": f"m{i}", "content": f"事实{i}", "layer": "semantic"}
            for i in range(5)
        ]
        + [
            {
                "id": "r1",
                "content": existing_text,
                "layer": "episodic",
                "category": "reflection",
                "source": "reflection",
                "memory_kind": "reflection",
            }
        ]
    )
    near = "铭君会主动去了解我的原作，比如看Bilibili视频，这让我感到被认真对待和关心，也有点开心。"
    api = _RecordingApiClient(
        '{"reflections":[{"content":"' + near + '","importance":0.7,"confidence":0.8}]}'
    )
    reflector = MemoryReflector(api, store)  # type: ignore[arg-type]
    result = reflector.reflect(memory_store=store)  # type: ignore[arg-type]
    assert result.memories_created == 0
    assert store.created == []


def test_auto_recall_skips_reflections() -> None:
    selected = _select_memories(
        [
            {
                "id": "f1",
                "content": "他喜欢抹茶",
                "score": 0.9,
                "source": "self_curation",
                "metadata": {"importance": 0.8},
            },
            {
                "id": "r1",
                "content": "我意识到他最近更依赖我",
                "score": 0.99,
                "source": "reflection",
                "category": "reflection",
                "metadata": {
                    "importance": 0.9,
                    "source": "reflection",
                    "category": "reflection",
                    "memory_kind": "reflection",
                },
            },
        ],
        threshold=0.3,
        limit=5,
    )
    assert [m["id"] for m in selected] == ["f1"]


def test_curator_formats_reflections_as_non_facts() -> None:
    text = _format_existing_memories(
        [
            {"id": "1", "content": "他喜欢抹茶", "layer": "semantic"},
            {
                "id": "2",
                "content": "我意识到他最近更忙",
                "layer": "episodic",
                "category": "reflection",
                "source": "reflection",
            },
        ]
    )
    assert "事实与事件" in text
    assert "独处感想" in text
    assert "非事实" in text
    assert memory_record_is_reflection(
        {"source": "reflection", "category": "reflection"}
    )


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
            {"system_prompt": system_prompt, "messages": messages, "kwargs": kwargs}
        )
        if not self.responses:
            return "{}"
        return self.responses.pop(0)
