"""时间敏感：相对年龄、runtime.time、召回/会话摘要标注。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.agent.context_orchestrator import _builtin_fragments, build_context_request
from app.agent.memory_recall import _annotate_recalled_memory_content
from app.agent.session_state_context import _render_line, build_session_state_fragment
from app.agent.time_awareness import (
    annotate_with_relative_age,
    format_duration_zh,
    format_local_time_context,
    format_relative_age,
)
from app.llm.api_client import ChatMessage
from app.storage.chat_history import ChatHistoryEntry
from app.storage.history_digest import DigestLine


def test_format_relative_age_buckets() -> None:
    now = datetime(2026, 7, 20, 22, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    assert format_relative_age((now - timedelta(seconds=30)).isoformat(), now=now) == "刚才"
    assert format_relative_age((now - timedelta(minutes=12)).isoformat(), now=now) == "12分钟前"
    assert format_relative_age((now - timedelta(hours=2)).isoformat(), now=now) == "约2小时前"
    assert format_relative_age((now - timedelta(hours=8)).isoformat(), now=now) == "今天稍早"
    assert format_relative_age((now - timedelta(days=1)).isoformat(), now=now) == "昨天"
    assert format_relative_age((now - timedelta(days=3)).isoformat(), now=now) == "约3天前"
    assert format_relative_age((now - timedelta(days=14)).isoformat(), now=now) == "约2周前"
    assert format_relative_age("not-a-time", now=now) == ""


def test_format_duration_and_local_time_context() -> None:
    assert "分钟" in format_duration_zh(150)
    text = format_local_time_context(
        "2026-07-20T21:43:00+08:00",
        seconds_since_interaction=600,
    )
    assert "当前本地时间" in text
    assert "晚上" in text
    assert "距上次和对方互动约 10 分钟" in text
    short = format_local_time_context(
        "2026-07-20T21:43:00+08:00",
        seconds_since_interaction=30,
    )
    assert "距上次" not in short


def test_annotate_memory_with_age_and_expired() -> None:
    now = datetime(2026, 7, 20, 22, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    stamped = annotate_with_relative_age(
        "我们约好十二点前不提休息",
        (now - timedelta(days=2)).isoformat(),
        now=now,
    )
    assert stamped.startswith("（约2天前）")
    expired = annotate_with_relative_age(
        "旧约定",
        (now - timedelta(days=8)).isoformat(),
        now=now,
        expired=True,
    )
    assert "已过期的约定" in expired
    assert "约1周前" in expired


def test_runtime_time_fragment_includes_gap() -> None:
    request = build_context_request(
        [ChatMessage(role="user", content="在吗")],
        source="chat",
        mode="normal",
        event_type="",
        step_index=0,
        remaining_steps=0,
        available_tools=(),
        event_payload={"seconds_since_pet_interaction": 900},
        current_time="2026-07-20T22:00:00+08:00",
    )
    fragments = _builtin_fragments(request)
    time_frag = next(item for item in fragments if item.fragment_id == "runtime.time")
    assert "距上次和对方互动约 15 分钟" in time_frag.content
    assert "时段" in time_frag.content


def test_recall_annotation_uses_created_at() -> None:
    now = datetime(2026, 7, 20, 22, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    text = _annotate_recalled_memory_content(
        {
            "content": "铭君喜欢抹茶",
            "created_at": (now - timedelta(days=4)).isoformat(),
            "metadata": {},
        },
        now=now,
    )
    assert text.startswith("（约4天前）")
    assert "抹茶" in text


def test_session_digest_line_includes_relative_age() -> None:
    now = datetime.now().astimezone()
    line = DigestLine(
        role="user",
        content="刚才在路上看到一只猫。",
        channel="mobile",
        created_at=(now - timedelta(hours=2)).isoformat(),
    )
    rendered = _render_line(line)
    assert "约2小时前" in rendered
    assert "对方（手机）" in rendered


def test_session_fragment_keeps_mobile_markers() -> None:
    now = datetime.now().astimezone()
    entries = [
        ChatHistoryEntry(
            (now - timedelta(hours=3)).isoformat(),
            "user",
            "刚才在路上看到一只猫。",
            channel="mobile",
        ),
        ChatHistoryEntry(
            (now - timedelta(hours=3) + timedelta(seconds=5)).isoformat(),
            "assistant",
            "是怎样的猫？",
            channel="mobile",
        ),
    ]
    fragment = build_session_state_fragment(entries)
    assert fragment is not None
    assert "对方（手机）" in fragment.content
    assert "约3小时前" in fragment.content or "小时前" in fragment.content
