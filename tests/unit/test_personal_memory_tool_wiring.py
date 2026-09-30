"""Which memory tools the personal plugin exposes, and how the boundary gates them."""

from __future__ import annotations

import threading

import pytest

from test_personal_memory_backend import dependencies  # noqa: F401 - pytest fixture
from test_personal_completed_curation import rehearsal  # noqa: F401 - pytest fixture
from plugins.builtin.sakura_mem0.personal_runtime import PersonalRecallBoundary
from plugins.builtin.sakura_mem0.plugin import SakuraMem0Runtime, _personal_tool_registrations

WRITE_TOOLS = {"memory_remember", "memory_update", "memory_forget", "memory_let_go"}
READ_TOOLS = {"memory_search", "memory_detail", "memory_timeline"}


class _Boundary:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def memory_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        return {"ok": True}


def _runtime(tmp_path, boundary=None) -> SakuraMem0Runtime:
    return SakuraMem0Runtime(tmp_path, "alice", boundary=boundary or _Boundary())


def test_daily_mode_exposes_the_full_personal_tool_set(tmp_path) -> None:
    boundary = _Boundary()
    runtime = _runtime(tmp_path, boundary)
    registrations = _personal_tool_registrations(runtime, daily=True)

    assert {descriptor["name"] for descriptor, _ in registrations} == READ_TOOLS | WRITE_TOOLS
    callbacks = {descriptor["name"]: callback for descriptor, callback in registrations}
    assert all(callback.__self__ is runtime for callback in callbacks.values())
    callbacks["memory_let_go"]({"memory_id": "m1"})
    assert boundary.calls == [("let_go", {"memory_id": "m1"})]


def test_read_only_modes_expose_search_only(tmp_path) -> None:
    names = {descriptor["name"] for descriptor, _ in _personal_tool_registrations(_runtime(tmp_path), daily=False)}

    assert names == {"memory_search"}


def test_search_descriptor_offers_index_mode_and_released_opt_in(tmp_path) -> None:
    descriptor = next(d for d, _ in _personal_tool_registrations(_runtime(tmp_path), daily=True)
                      if d["name"] == "memory_search")

    properties = descriptor["parameters"]["properties"]
    assert properties["mode"]["enum"] == ["full", "index"]
    assert properties["include_released"]["type"] == "boolean"


def _bare_boundary(*, daily: bool, status: str = "ready", records=None) -> PersonalRecallBoundary:
    boundary = object.__new__(PersonalRecallBoundary)
    boundary._lock = threading.RLock()
    boundary._status = status
    boundary._records = records
    boundary._daily = daily
    boundary.scope = "alice"
    return boundary


def test_boundary_reports_loading_instead_of_raising() -> None:
    boundary = _bare_boundary(daily=True, status="loading")

    assert boundary.memory_tool("remember", {"content": "x"}) == {"status": "loading", "ok": False}
    assert boundary.memory_tool("search", {"query": "x"}) == {"status": "loading", "memories": []}


def test_boundary_refuses_writes_outside_daily_mode() -> None:
    with pytest.raises(ValueError, match="READ_ONLY"):
        _bare_boundary(daily=False, records=object()).memory_tool("remember", {"content": "x"})


def test_daily_plugin_writes_searches_and_lets_go_through_registered_tools(rehearsal) -> None:
    import json

    from test_personal_completed_curation import start

    context, _timeline, _calls, _client = rehearsal
    memory = context.root / "data/memory"
    (memory / ".personal-write-rehearsal.json").unlink()
    (memory / ".personal-daily.json").write_text(json.dumps({
        "schemaVersion": 1, "purpose": "personal-memory-daily",
        "root": str(memory.resolve()), "scopes": ["sakura"],
    }), encoding="utf-8")
    start(context, daily=True)
    tools = {descriptor["name"]: callback for descriptor, callback in context.tools}

    saved = tools["memory_remember"]({"content": "synthetic: likes quiet mornings"})
    key = saved["memory"]["id"]
    assert key in [item["id"] for item in tools["memory_search"]({"query": "mornings"})["memories"]]

    assert tools["memory_let_go"]({"memory_id": key})["ok"] is True
    assert key not in [item["id"] for item in tools["memory_search"]({"query": "mornings"})["memories"]]


def test_rehearsal_plugin_keeps_memory_read_only(rehearsal) -> None:
    from test_personal_completed_curation import start

    context, _timeline, _calls, _client = rehearsal
    start(context, daily=False)

    assert [descriptor["name"] for descriptor, _ in context.tools] == ["memory_search"]


def test_boundary_rejects_unknown_tool_names() -> None:
    with pytest.raises(ValueError):
        _bare_boundary(daily=True, records=object()).memory_tool("_store", {})
