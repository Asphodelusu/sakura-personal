"""Explicit memory tools of the personal plugin, on a real personal store."""

from __future__ import annotations

import pytest

from test_personal_memory_backend import dependencies, Encoder  # noqa: F401 - pytest fixture
from test_personal_memory_records import fixture_memory
from plugins.builtin.sakura_mem0 import personal_records as records
from plugins.builtin.sakura_mem0.boundary import _project_memory
from plugins.builtin.sakura_mem0.memory_recall import _select_memories
from plugins.builtin.sakura_mem0.personal_tools import PersonalMemoryTools

SCOPE = "alice"


@pytest.fixture
def tools(tmp_path, dependencies):  # noqa: F811
    root, identity = fixture_memory(tmp_path, dependencies)
    store = records.open_personal_memory(root, identity=identity, encoder=Encoder(384))
    try:
        yield PersonalMemoryTools(store, SCOPE), store
    finally:
        store.close()


def _ids(result) -> list[str]:
    return [item["id"] for item in result["memories"]]


def test_remember_then_search_finds_explicit_memory(tools) -> None:
    tool, _store = tools
    saved = tool.remember({"content": "synthetic: likes rainy days", "layer": "semantic"})

    assert saved["ok"] is True and saved["memory"]["source"] == "explicit"
    assert _ids(tool.search({"query": "rainy"})) == [saved["memory"]["id"]]


def test_let_go_hides_memory_from_search_and_recall_without_deleting(tools) -> None:
    tool, store = tools
    key = tool.remember({"content": "synthetic: an old worry"})["memory"]["id"]

    assert tool.let_go({"memory_id": key})["ok"] is True

    assert _ids(tool.search({"query": "worry"})) == []
    assert _ids(tool.search({"query": "worry", "include_released": True})) == [key]
    raw = store.get(SCOPE, key)
    assert raw is not None
    assert _select_memories([_project_memory(raw, SCOPE)], 0.0, 5) == []


def test_update_keeps_id_and_forget_removes(tools) -> None:
    tool, store = tools
    key = tool.remember({"content": "synthetic: plans a trip"})["memory"]["id"]

    updated = tool.update({"memory_id": key, "content": "synthetic: trip postponed"})
    assert updated["memory"]["id"] == key and updated["memory"]["content"] == "synthetic: trip postponed"

    assert tool.forget({"memory_id": key})["ok"] is True
    assert store.get(SCOPE, key) is None


def test_detail_expands_known_ids_and_reports_missing(tools) -> None:
    tool, _store = tools
    key = tool.remember({"content": "synthetic: favourite tea"})["memory"]["id"]

    result = tool.detail({"ids": f"{key}, missing-id"})

    assert [item["id"] for item in result["memories"]] == [key]
    assert result["missing"] == ["missing-id"]


def test_index_mode_returns_titles_not_bodies(tools) -> None:
    tool, _store = tools
    tool.remember({"content": "synthetic: " + "long detail " * 20})

    item = tool.search({"query": "detail", "mode": "index"})["memories"][0]

    assert set(item) == {"id", "title", "layer", "created_at", "importance", "approx_tokens"}
    assert len(item["title"]) <= 44


def test_timeline_returns_neighbours_in_creation_order(tools) -> None:
    tool, _store = tools
    keys = [tool.remember({"content": f"synthetic: event {index}"})["memory"]["id"] for index in range(5)]
    # Writes share a seconds-precision timestamp; ties fall back to the memory id.
    ordered = [item["id"] for item in sorted(
        tool.detail({"ids": keys})["memories"], key=lambda item: (item["createdAt"], item["id"]))]
    anchor = ordered[2]

    result = tool.timeline({"memory_id": anchor, "before": 1, "after": 1})

    assert [item["id"] for item in result["memories"]] == ordered[1:4]
    assert result["anchor"] == anchor
    edge = tool.timeline({"memory_id": ordered[0], "before": 2, "after": 0})
    assert [item["id"] for item in edge["memories"]] == ordered[:1]


def test_sensitive_and_core_profile_writes_are_refused(tools) -> None:
    tool, _store = tools

    with pytest.raises(ValueError):
        tool.remember({"content": "my password is hunter2hunter2"})
    with pytest.raises(ValueError):
        tool.remember({"content": "synthetic", "layer": "core_profile"})
    with pytest.raises(ValueError):
        tool.let_go({"memory_id": "core_profile:alice"})


def test_model_cannot_choose_another_scope(tools) -> None:
    tool, _store = tools

    with pytest.raises(ValueError):
        tool.remember({"content": "synthetic", "scope": "bob"})
