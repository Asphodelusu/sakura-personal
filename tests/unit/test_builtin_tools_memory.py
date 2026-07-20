from __future__ import annotations

from app.agent.builtin_tools import _memory_update_arguments, create_builtin_tool_registry
from app.agent.memory import MemoryStore


class FakeMem0:
    def __init__(self) -> None:
        self._memories: dict[str, dict[str, object]] = {}

    def add(self, content: str, *, user_id: str, metadata: dict[str, object], infer: bool = False) -> dict[str, object]:
        memory_id = f"mem-{len(self._memories) + 1}"
        memory = {"id": memory_id, "content": content, "memory": content, "metadata": metadata}
        self._memories[memory_id] = memory
        return {"results": [memory]}

    def get(self, memory_id: str) -> dict[str, object] | None:
        return self._memories.get(memory_id)

    def update(self, memory_id: str, content: str, *, metadata: dict[str, object]) -> dict[str, object]:
        memory = self._memories[memory_id]
        memory["content"] = content
        memory["memory"] = content
        memory["metadata"] = metadata
        return {"results": [memory]}

    def delete(self, memory_id: str) -> None:
        self._memories.pop(memory_id, None)


def test_memory_update_arguments_maps_new_content_alias() -> None:
    mapped = _memory_update_arguments(
        {
            "memory_id": "802392db-test",
            "new_content": "更新后的记忆内容",
        }
    )
    assert mapped == {"id": "802392db-test", "content": "更新后的记忆内容"}


def test_builtin_memory_update_accepts_new_content_alias() -> None:
    fake = FakeMem0()
    registry = create_builtin_tool_registry(
        __import__("pathlib").Path("test_builtin_memory_update_alias"),
        memory=MemoryStore(memory_client=fake),
    )
    remember_result = registry.execute("memory_remember", {"content": "对方喜欢热咖啡"})
    assert remember_result.success
    memory = remember_result.content["memory"]
    assert memory["metadata"]["source"] == "explicit"
    memory_id = memory["id"]

    update_result = registry.execute(
        "memory_update",
        {
            "memory_id": memory_id,
            "new_content": "对方喜欢低糖热咖啡",
        },
    )

    assert update_result.success
    assert update_result.content["memory"]["content"] == "对方喜欢低糖热咖啡"
