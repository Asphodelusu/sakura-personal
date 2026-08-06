"""history_search / history_read 工具处理与注册。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.builtin_tools import create_builtin_tool_registry, create_mobile_tool_registry
from app.agent.history_tools import (
    HistoryStoreRef,
    handle_history_read,
    handle_history_search,
)
from app.agent.memory import MemoryStore
from app.storage.chat_history import ChatHistoryStore


@pytest.fixture()
def store(tmp_path: Path) -> ChatHistoryStore:
    s = ChatHistoryStore(tmp_path / "Sakura.jsonl", assistant_name="桜")
    yield s
    s.close()


def _seed(store: ChatHistoryStore) -> int:
    with store._lock:
        store._conn.execute(
            """
            INSERT INTO chat_history
                (created_at, role, content, translation, tone, portrait, channel, debug)
            VALUES
                ('2026-07-20T10:00:00+08:00', 'user', '喜欢喝咖啡', '', '', '', '', ''),
                ('2026-07-20T10:01:00+08:00', 'assistant', 'わかる', '懂了', '', '', '', ''),
                ('2026-07-20T10:02:00+08:00', 'system', '内部', '', '', '', '', '')
            """
        )
        row = store._conn.execute(
            "SELECT id FROM chat_history WHERE content = '喜欢喝咖啡'"
        ).fetchone()
        return int(row["id"])


def test_handle_history_search_and_read(store: ChatHistoryStore) -> None:
    anchor = _seed(store)
    ref = HistoryStoreRef(store)
    searched = handle_history_search(ref, {"keyword": "咖啡", "limit": 10})
    assert searched["count"] == 1
    assert searched["total_count"] == 1
    assert searched["offset"] == 0
    assert searched["entries"][0]["id"] == anchor
    assert "咖啡" in searched["entries"][0]["content"]

    read = handle_history_read(ref, {"entry_id": anchor, "before": 0, "after": 1})
    assert read["target"]["id"] == anchor
    assert read["after"][0]["content"] == "わかる"
    assert read["count"] == 2


def test_handle_search_pagination_hint(store: ChatHistoryStore) -> None:
    with store._lock:
        for i in range(25):
            store._conn.execute(
                """
                INSERT INTO chat_history
                    (created_at, role, content, translation, tone, portrait, channel, debug)
                VALUES (?, 'user', ?, '', '', '', '', '')
                """,
                (f"2026-07-20T10:{i:02d}:00+08:00", f"出去玩-{i}"),
            )
    ref = HistoryStoreRef(store)
    page1 = handle_history_search(ref, {"keyword": "出去玩", "limit": 10, "offset": 0})
    assert page1["total_count"] == 25
    assert page1["has_more"] is True
    assert page1["next_offset"] == 10
    assert "offset=10" in page1["agent_hint"]
    assert page1["entries"][0]["content"].startswith("出去玩-0")

    page2 = handle_history_search(ref, {"keyword": "出去玩", "limit": 10, "offset": 10})
    assert page2["offset"] == 10
    assert page2["has_more"] is True
    assert page2["entries"][0]["content"].startswith("出去玩-10")


def test_handle_degrades_without_store() -> None:
    ref = HistoryStoreRef(None)
    searched = handle_history_search(ref, {})
    assert searched["error"]
    assert searched["entries"] == []
    assert searched["has_more"] is False
    assert searched["total_count"] == 0

    read = handle_history_read(None, {"entry_id": 1})
    assert read["error"]
    assert read["target"] is None


def test_history_store_ref_switch_updates_lookup(tmp_path: Path) -> None:
    store_a = ChatHistoryStore(tmp_path / "A.jsonl", "A")
    store_b = ChatHistoryStore(tmp_path / "B.jsonl", "B")
    try:
        with store_a._lock:
            store_a._conn.execute(
                "INSERT INTO chat_history "
                "(created_at, role, content, translation, tone, portrait, channel, debug) "
                "VALUES ('2026-07-20T10:00:00+08:00', 'user', '角色A独有', '', '', '', '', '')"
            )
        with store_b._lock:
            store_b._conn.execute(
                "INSERT INTO chat_history "
                "(created_at, role, content, translation, tone, portrait, channel, debug) "
                "VALUES ('2026-07-20T10:00:00+08:00', 'user', '角色B独有', '', '', '', '', '')"
            )
        ref = HistoryStoreRef(store_a)
        hit_a = handle_history_search(ref, {"keyword": "角色A"})
        assert hit_a["count"] == 1
        miss_b = handle_history_search(ref, {"keyword": "角色B"})
        assert miss_b["count"] == 0

        ref.store = store_b
        hit_b = handle_history_search(ref, {"keyword": "角色B"})
        assert hit_b["count"] == 1
        miss_a = handle_history_search(ref, {"keyword": "角色A"})
        assert miss_a["count"] == 0
    finally:
        store_a.close()
        store_b.close()


def test_builtin_and_mobile_register_history_tools(tmp_path: Path, store: ChatHistoryStore) -> None:
    ref = HistoryStoreRef(store)
    memory = MemoryStore(base_dir=tmp_path)
    desktop = create_builtin_tool_registry(tmp_path, memory=memory, history=ref)
    assert desktop.get("history_search") is not None
    assert desktop.get("history_read") is not None
    result = desktop.get("history_search").handler({"keyword": "不存在的词xyz"})
    assert result["count"] == 0

    mobile = create_mobile_tool_registry(memory, ref)
    assert mobile.get("history_search") is not None
    assert mobile.get("history_read") is not None


def test_builtin_without_history_degrades(tmp_path: Path) -> None:
    registry = create_builtin_tool_registry(tmp_path)
    tool = registry.get("history_search")
    assert tool is not None
    result = tool.handler({})
    assert "不可用" in result["error"]
