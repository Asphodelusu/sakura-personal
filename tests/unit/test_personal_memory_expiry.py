"""Recall must honor the expiry fields old personal memories carry after projection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from plugins.builtin.sakura_mem0.boundary import _project_memory
from plugins.builtin.sakura_mem0.domain_types import ContextRequest
from plugins.builtin.sakura_mem0.memory_recall import MemoryRecallService

SCOPE = "Sakura"


def _stamp(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _raw(memory_id: str, content: str, **metadata: object) -> dict[str, object]:
    return {
        "id": memory_id,
        "memory": content,
        "user_id": SCOPE,
        "score": 0.9,
        "metadata": {"source": "inferred", **metadata},
    }


class _ProjectedMemory:
    """Returns memories in the shape the personal and plugin boundaries publish."""

    def __init__(self, *raws: dict[str, object]) -> None:
        self._memories = [_project_memory(raw, SCOPE) for raw in raws]

    def search_memory(self, arguments, *, wait=False):
        return {"status": "ready", "memories": list(self._memories)}


def _recalled_ids(memory: _ProjectedMemory, *, turn_id: str = "") -> list[str]:
    result = MemoryRecallService(memory).recall(
        ContextRequest(current_input="最近怎么样", current_turn_id=turn_id)
    )
    return [fragment.metadata["memory_id"] for fragment in result.fragments]


def test_projection_keeps_expiry_fields_from_metadata() -> None:
    valid_until, expires_at = _stamp(-1), _stamp(3)
    projected = _project_memory(
        _raw("m1", "synthetic status", valid_until=valid_until, expires_at=expires_at),
        SCOPE,
    )

    assert projected is not None
    assert projected["validUntil"] == valid_until
    assert projected["expiresAt"] == expires_at


def test_recall_skips_projected_memory_past_valid_until() -> None:
    memory = _ProjectedMemory(
        _raw("past", "synthetic finished plan", valid_until=_stamp(-1)),
        _raw("future", "synthetic ongoing plan", valid_until=_stamp(2)),
        _raw("plain", "synthetic stable preference"),
    )

    assert _recalled_ids(memory) == ["future", "plain"]


def test_recall_skips_projected_memory_past_expires_at() -> None:
    memory = _ProjectedMemory(
        _raw("expired", "synthetic short-lived status", expires_at=_stamp(-2)),
        _raw("kept", "synthetic stable preference"),
    )

    assert _recalled_ids(memory) == ["kept"]


def test_recall_filters_projected_memory_created_in_current_turn() -> None:
    memory = _ProjectedMemory(
        _raw("same-turn", "synthetic new fact", created_in_turn_id="turn-now"),
        _raw("older", "synthetic older fact", created_in_turn_id="turn-before"),
    )

    assert _recalled_ids(memory, turn_id="turn-now") == ["older"]
