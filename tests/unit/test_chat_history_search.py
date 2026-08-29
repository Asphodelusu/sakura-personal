"""ChatHistoryStore.search_between / context_around 与 id 暴露。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.storage.chat_history import ChatHistoryStore


@pytest.fixture()
def store(tmp_path: Path) -> ChatHistoryStore:
    path = tmp_path / "Sakura.jsonl"
    s = ChatHistoryStore(path, assistant_name="桜")
    yield s
    s.close()


def _seed(store: ChatHistoryStore, rows: list[tuple[str, str, str, str]]) -> list[int]:
    """写入 (created_at, role, content, translation)，返回 id 列表。"""
    ids: list[int] = []
    for created_at, role, content, translation in rows:
        with store._lock:
            cur = store._conn.execute(
                """
                INSERT INTO chat_history
                    (created_at, role, content, translation, tone, portrait, channel, debug)
                VALUES (?, ?, ?, ?, '', '', '', '')
                """,
                (created_at, role, content, translation),
            )
            ids.append(int(cur.lastrowid))
    return ids


def test_load_paths_include_id(store: ChatHistoryStore) -> None:
    ids = _seed(
        store,
        [
            ("2026-07-20T10:00:00+08:00", "user", "你好", ""),
            ("2026-07-20T10:01:00+08:00", "assistant", "こんにちは", "你好"),
        ],
    )
    loaded = store.load()
    assert [e.id for e in loaded] == ids
    assert all(e.id > 0 for e in loaded)

    tail, _ = store.load_tail(10)
    assert [e.id for e in tail] == ids

    older, has_more = store.load_older(skip_last=1, limit=10)
    assert [e.id for e in older] == ids[:1]
    assert has_more is False


def test_count_and_load_slice_offset(store: ChatHistoryStore) -> None:
    ids = _seed(
        store,
        [
            ("2026-07-20T10:00:00+08:00", "user", "一", ""),
            ("2026-07-20T10:01:00+08:00", "assistant", "二", "二"),
            ("2026-07-20T10:02:00+08:00", "user", "三", ""),
        ],
    )
    assert store.count() == 3
    sliced = store.load_slice(1)
    assert [e.id for e in sliced] == ids[1:]
    limited = store.load_slice(1, limit=1)
    assert [e.id for e in limited] == ids[1:2]


def test_created_at_index_exists(store: ChatHistoryStore) -> None:
    rows = store._conn.execute("PRAGMA index_list('chat_history')").fetchall()
    names = {row["name"] for row in rows}
    assert "idx_chat_history_created_at" in names


def test_search_by_time_range(store: ChatHistoryStore) -> None:
    _seed(
        store,
        [
            ("2026-07-19T10:00:00+08:00", "user", "昨天的话", ""),
            ("2026-07-20T10:00:00+08:00", "user", "今天的话", ""),
            ("2026-07-20T12:00:00+08:00", "assistant", "回复", "回复"),
        ],
    )
    entries, has_more, total = store.search_between(
        start="2026-07-20T00:00:00+08:00",
        end="2026-07-20T23:59:59+08:00",
        limit=20,
    )
    assert has_more is False
    assert total == 2
    assert [e.content for e in entries] == ["今天的话", "回复"]


def test_search_by_keyword_content_or_translation(store: ChatHistoryStore) -> None:
    _seed(
        store,
        [
            ("2026-07-20T10:00:00+08:00", "user", "聊聊原神", ""),
            ("2026-07-20T10:01:00+08:00", "assistant", "うん", "原神好玩"),
            ("2026-07-20T10:02:00+08:00", "user", "无关", ""),
        ],
    )
    entries, _, total = store.search_between(keyword="原神", limit=20)
    assert total == 2
    # 有关键词：ASC，最早命中在前
    assert [e.content for e in entries] == ["聊聊原神", "うん"]


def test_search_time_and_keyword(store: ChatHistoryStore) -> None:
    _seed(
        store,
        [
            ("2026-07-19T10:00:00+08:00", "user", "旧话题咖啡", ""),
            ("2026-07-20T10:00:00+08:00", "user", "新话题咖啡", ""),
        ],
    )
    entries, _, total = store.search_between(
        start="2026-07-20T00:00:00+08:00",
        keyword="咖啡",
        limit=20,
    )
    assert total == 1
    assert [e.content for e in entries] == ["新话题咖啡"]


def test_search_no_filter_returns_recent(store: ChatHistoryStore) -> None:
    _seed(
        store,
        [
            (f"2026-07-20T10:0{i}:00+08:00", "user", f"msg{i}", "")
            for i in range(5)
        ],
    )
    entries, has_more, total = store.search_between(limit=3)
    assert total == 5
    assert len(entries) == 3
    # 无筛选：最近页，页内正序
    assert [e.content for e in entries] == ["msg2", "msg3", "msg4"]
    assert has_more is True


def test_search_window_asc_keeps_topic_start(store: ChatHistoryStore) -> None:
    """时间窗内正序分页：话题开头不会被窗尾新消息挤掉。"""
    rows = []
    for i in range(16):
        rows.append(
            (f"2026-07-20T01:{i:02d}:00+08:00", "user", f"出去玩-{i}", "")
        )
    for i in range(16):
        rows.append(
            (f"2026-07-20T01:{30 + i:02d}:00+08:00", "user", f"别的话题-{i}", "")
        )
    _seed(store, rows)

    page1, has_more, total = store.search_between(
        start="2026-07-20T01:00:00+08:00",
        end="2026-07-20T01:59:59+08:00",
        limit=20,
        offset=0,
    )
    assert total == 32
    assert has_more is True
    assert [e.content for e in page1] == [f"出去玩-{i}" for i in range(16)] + [
        f"别的话题-{i}" for i in range(4)
    ]

    page2, has_more2, _ = store.search_between(
        start="2026-07-20T01:00:00+08:00",
        end="2026-07-20T01:59:59+08:00",
        limit=20,
        offset=20,
    )
    assert has_more2 is False
    assert [e.content for e in page2] == [f"别的话题-{i}" for i in range(4, 16)]


def test_search_keyword_asc_offset_keeps_early_hits(store: ChatHistoryStore) -> None:
    """关键词命中 > limit 时 ASC+offset 可翻到最早命中。"""
    _seed(
        store,
        [
            (f"2026-07-20T10:{i:02d}:00+08:00", "user", f"出去玩第{i}句", "")
            for i in range(30)
        ],
    )
    page1, has_more, total = store.search_between(keyword="出去玩", limit=20, offset=0)
    assert total == 30
    assert has_more is True
    assert page1[0].content == "出去玩第0句"
    assert page1[-1].content == "出去玩第19句"

    page2, has_more2, _ = store.search_between(keyword="出去玩", limit=20, offset=20)
    assert has_more2 is False
    assert [e.content for e in page2] == [f"出去玩第{i}句" for i in range(20, 30)]


def test_search_like_escape(store: ChatHistoryStore) -> None:
    _seed(
        store,
        [
            ("2026-07-20T10:00:00+08:00", "user", "100%完成", ""),
            ("2026-07-20T10:01:00+08:00", "user", "100完成", ""),
            ("2026-07-20T10:02:00+08:00", "user", "a_b", ""),
        ],
    )
    entries, _, _ = store.search_between(keyword="100%", limit=20)
    assert [e.content for e in entries] == ["100%完成"]
    entries2, _, _ = store.search_between(keyword="a_b", limit=20)
    assert [e.content for e in entries2] == ["a_b"]


def test_search_excludes_system(store: ChatHistoryStore) -> None:
    _seed(
        store,
        [
            ("2026-07-20T10:00:00+08:00", "system", "内部标记", ""),
            ("2026-07-20T10:01:00+08:00", "user", "你好", ""),
            ("2026-07-20T10:02:00+08:00", "error", "出错了", ""),
        ],
    )
    entries, _, total = store.search_between(limit=20)
    assert total == 1
    assert [e.role for e in entries] == ["user"]


def test_search_limit_hard_cap(store: ChatHistoryStore) -> None:
    _seed(
        store,
        [
            (f"2026-07-20T10:{i:02d}:00+08:00", "user", f"m{i}", "")
            for i in range(60)
        ],
    )
    entries, has_more, total = store.search_between(limit=999)
    assert total == 60
    assert len(entries) == 50
    assert has_more is True


def test_context_around(store: ChatHistoryStore) -> None:
    ids = _seed(
        store,
        [
            ("2026-07-20T10:00:00+08:00", "user", "a", ""),
            ("2026-07-20T10:01:00+08:00", "assistant", "b", ""),
            ("2026-07-20T10:02:00+08:00", "user", "c", ""),
            ("2026-07-20T10:03:00+08:00", "assistant", "d", ""),
            ("2026-07-20T10:04:00+08:00", "user", "e", ""),
        ],
    )
    result = store.context_around(ids[2], before=1, after=2)
    assert result["anchor_id"] == ids[2]
    assert result["target"].content == "c"
    assert [e.content for e in result["before"]] == ["b"]
    assert [e.content for e in result["after"]] == ["d", "e"]


def test_context_around_invalid_id(store: ChatHistoryStore) -> None:
    result = store.context_around(0)
    assert result["target"] is None
    assert "error" in result

    result2 = store.context_around(-3)
    assert result2["target"] is None
    assert "error" in result2

    result3 = store.context_around(99999)
    assert result3["target"] is None
    assert "hint" in result3


def test_load_after_id_keeps_ascending_ids_and_channels(store: ChatHistoryStore) -> None:
    first_id = store.append("assistant", "明日の昼、どうする？", channel="proactive")
    second_id = store.append("user", "听到了，还看情况")
    third_id = store.append("assistant", "作るなら教えて", channel="")
    fourth_id = store.append("user", "稍后")

    rows = store.load_after_id(first_id, limit=2)
    assert [row.id for row in rows] == [second_id, third_id]
    assert rows[0].role == "user"
    assert rows[0].channel == ""
    assert rows[1].role == "assistant"
    assert rows[1].channel == ""

    all_later = store.load_after_id(first_id, limit=50)
    assert [row.id for row in all_later] == [second_id, third_id, fourth_id]
    assert store.load_after_id(fourth_id) == []
    assert store.load_after_id(0) == []
    assert store.load_after_id(-1) == []
