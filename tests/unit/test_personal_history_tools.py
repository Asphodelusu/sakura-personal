"""history_search / history_read over the current character's Timeline."""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

import pytest

from app.agent.tools import ToolRegistry
from app.core_host.history_tools import create_history_tools
from app.storage.paths import StoragePaths
from app.storage.timeline import NewTimelineEntry, TimelineKind, TimelineStore


def _stamp(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def _segment(text: str, translation: str) -> dict:
    return {"text": text, "translation": translation, "tone": "", "portrait": "", "suppressTts": False}


def _root(tmp_path: Path) -> Path:
    store = TimelineStore(StoragePaths(tmp_path).timeline_database())
    store.initialize()
    rows = [
        ("sakura", TimelineKind.HUMAN, {"text": "\u6628\u5929\u8bf4\u7684\u6a31\u82b1\u5f88\u597d\u770b"}, _stamp(days=1, hours=2)),
        ("sakura", TimelineKind.ASSISTANT, {"segments": [
            _segment("\u685c\u3001\u304d\u308c\u3044\u3060\u3063\u305f\u306d\u3002", "\u6a31\u82b1\u5f88\u7f8e\u5462\u3002")]},
         _stamp(days=1, hours=2)),
        ("sakura", TimelineKind.OBSERVATION, {"text": "screen summary"}, _stamp(hours=5)),
        ("sakura", TimelineKind.HUMAN, {"text": "\u4eca\u5929\u5403\u4e86\u62c9\u9762"}, _stamp(minutes=60)),
        ("sakura", TimelineKind.ASSISTANT, {"segments": [
            _segment("\u30e9\u30fc\u30e1\u30f3\uff01", "\u62c9\u9762\uff01")]}, _stamp(minutes=59)),
        ("other", TimelineKind.HUMAN, {"text": "\u6a31\u82b1 from another character"}, _stamp(minutes=10)),
    ]
    for index, (character, kind, payload, created) in enumerate(rows):
        store.append(NewTimelineEntry(entry_id=f"e{index}", turn_id=f"t{index}", character_id=character,
                                      kind=kind, origin="chat", created_at=created, payload=payload))
    return tmp_path


def _tools(tmp_path: Path):
    tools = {tool.name: tool for tool in create_history_tools(_root(tmp_path), "sakura")}
    return tools["history_search"].handler, tools["history_read"].handler


def test_unfiltered_search_returns_recent_dialogue_of_this_character_only(tmp_path: Path) -> None:
    search, _read = _tools(tmp_path)

    result = search({})

    contents = [entry["content"] for entry in result["entries"]]
    assert result["total_count"] == 4
    assert all("another character" not in text and text != "screen summary" for text in contents)
    assert contents[-1] == "\u30e9\u30fc\u30e1\u30f3\uff01"
    assert [entry["role"] for entry in result["entries"]] == ["user", "assistant", "user", "assistant"]


def test_keyword_matches_text_or_translation_and_paginates(tmp_path: Path) -> None:
    search, _read = _tools(tmp_path)

    first = search({"keyword": "\u6a31\u82b1", "limit": 1})
    second = search({"keyword": "\u6a31\u82b1", "limit": 1, "offset": first["next_offset"]})

    assert first["total_count"] == 2 and first["has_more"] is True
    assert first["entries"][0]["role"] == "user"
    assert second["entries"][0]["translation"] == "\u6a31\u82b1\u5f88\u7f8e\u5462\u3002"
    assert second["has_more"] is False


def test_relative_time_window_compares_real_instants(tmp_path: Path) -> None:
    search, _read = _tools(tmp_path)

    # "约N小时前" is N hours ago plus or minus 30 minutes; Timeline stamps are UTC.
    result = search({"time": "\u7ea61\u5c0f\u65f6\u524d"})

    assert [entry["content"] for entry in result["entries"]] == [
        "\u4eca\u5929\u5403\u4e86\u62c9\u9762", "\u30e9\u30fc\u30e1\u30f3\uff01"]


def test_unparseable_time_reports_an_error(tmp_path: Path) -> None:
    search, _read = _tools(tmp_path)

    result = search({"time": "\u80e1\u8bf4\u516b\u9053"})

    assert result["entries"] == [] and "error" in result


def test_read_returns_dialogue_context_around_the_anchor(tmp_path: Path) -> None:
    search, read = _tools(tmp_path)
    anchor = search({"keyword": "\u62c9\u9762"})["entries"][0]["id"]

    result = read({"entry_id": anchor, "before": 1, "after": 1})

    assert result["target"]["content"] == "\u4eca\u5929\u5403\u4e86\u62c9\u9762"
    assert [entry["role"] for entry in result["before"]] == ["assistant"]
    assert [entry["role"] for entry in result["after"]] == ["assistant"]


def test_read_cannot_reach_another_characters_entry(tmp_path: Path) -> None:
    _search, read = _tools(tmp_path)
    foreign = TimelineStore(StoragePaths(tmp_path).timeline_database()).read_all("other")[0].seq

    result = read({"entry_id": foreign})

    assert result["target"] is None


def test_missing_timeline_is_reported_not_raised(tmp_path: Path) -> None:
    tools = {tool.name: tool for tool in create_history_tools(tmp_path / "empty", "sakura")}

    assert "error" in tools["history_search"].handler({})


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "runtime_v2" / "wp_3_01" / "ready"


def test_adapter_registers_history_tools(tmp_path: Path) -> None:
    from app.core_host.assistant_adapter import AssistantAdapter

    root = tmp_path / "root"
    shutil.copytree(FIXTURE_ROOT, root)
    registry = ToolRegistry()
    readiness = AssistantAdapter(root, tool_registry=registry, mcp_provider=None).initialize(Event())
    try:
        assert registry.get("history_search") is not None
        assert registry.get("history_read") is not None
    finally:
        readiness.session.runtime.close()
