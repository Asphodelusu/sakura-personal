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
    parse_memory_event_date,
    parse_relative_time_window,
)
from app.llm.api_client import ChatMessage
from app.storage.chat_history import ChatHistoryEntry
from app.storage.history_digest import DigestLine


def test_parse_memory_event_date_accepts_iso_and_date_only() -> None:
    now = datetime(2026, 7, 20, 22, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    assert parse_memory_event_date("2026-07-20", now=now).isoformat() == "2026-07-20"
    assert parse_memory_event_date("2026-07-19T22:00:00+08:00", now=now).isoformat() == "2026-07-19"
    assert parse_memory_event_date("not-a-date", now=now) is None


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


def test_parse_relative_time_window_phrases() -> None:
    now = datetime(2026, 7, 20, 22, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    assert parse_relative_time_window("", now=now) == (None, None)
    assert parse_relative_time_window("   ", now=now) == (None, None)
    assert parse_relative_time_window("不是时间", now=now) is None

    just_now = parse_relative_time_window("刚才", now=now)
    assert just_now is not None
    assert just_now[0] is not None and just_now[1] is not None
    assert just_now[0] < just_now[1]

    today = parse_relative_time_window("今天", now=now)
    assert today == ("2026-07-20T00:00:00+08:00", "2026-07-20T23:59:59+08:00")

    yesterday = parse_relative_time_window("昨天", now=now)
    assert yesterday == ("2026-07-19T00:00:00+08:00", "2026-07-19T23:59:59+08:00")

    day_before = parse_relative_time_window("前天", now=now)
    assert day_before == ("2026-07-18T00:00:00+08:00", "2026-07-18T23:59:59+08:00")

    minutes = parse_relative_time_window("12分钟前", now=now)
    assert minutes is not None
    # 中心 21:48，前后各 2 分钟 → 21:46 .. 21:50
    assert minutes[0] == "2026-07-20T21:46:00+08:00"
    assert minutes[1] == "2026-07-20T21:50:00+08:00"

    hours = parse_relative_time_window("约2小时前", now=now)
    assert hours is not None
    assert hours[0] < hours[1] <= now.isoformat(timespec="seconds")

    days = parse_relative_time_window("约3天前", now=now)
    assert days == ("2026-07-17T00:00:00+08:00", "2026-07-17T23:59:59+08:00")

    weeks = parse_relative_time_window("约2周前", now=now)
    assert weeks is not None
    assert weeks[0] < weeks[1]

    months = parse_relative_time_window("约1个月前", now=now)
    assert months is not None

    date_only = parse_relative_time_window("2026-07-15", now=now)
    assert date_only == ("2026-07-15T00:00:00+08:00", "2026-07-15T23:59:59+08:00")

    iso_point = parse_relative_time_window("2026-07-20T18:00:00+08:00", now=now)
    assert iso_point == ("2026-07-20T18:00:00+08:00", "2026-07-20T22:00:00+08:00")


def test_parse_relative_time_window_composed_period_and_clock() -> None:
    """相对日期 + 时段 + 时间点/区间。"""
    now = datetime(2026, 7, 20, 22, 0, 0, tzinfo=timezone(timedelta(hours=8)))

    # 用户核心场景：昨晚 1–2 点 → 次日凌晨（含完整问句前缀）
    night_range = parse_relative_time_window("昨天晚上大约一点到两点", now=now)
    assert night_range == (
        "2026-07-20T01:00:00+08:00",
        "2026-07-20T02:59:59+08:00",
    )
    full_ask = parse_relative_time_window(
        "我昨天晚上大约一点到两点和你聊了什么",
        now=now,
    )
    assert full_ask == night_range

    afternoon = parse_relative_time_window("昨天下午", now=now)
    assert afternoon == (
        "2026-07-19T12:00:00+08:00",
        "2026-07-19T18:00:00+08:00",
    )

    before_night = parse_relative_time_window("前天晚上十点", now=now)
    assert before_night == (
        "2026-07-18T22:00:00+08:00",
        "2026-07-18T22:59:59+08:00",
    )

    plain_range = parse_relative_time_window("昨天一点到两点", now=now)
    # 昨天 + 凌晨区间（1-2点）= 昨夜深夜 = 次日凌晨（2026-07-20 01:00）
    assert plain_range == (
        "2026-07-20T01:00:00+08:00",
        "2026-07-20T02:59:59+08:00",
    )

    # 2026-07-20 是周一；上周三 = 2026-07-15
    last_wed = parse_relative_time_window("上周三晚上", now=now)
    assert last_wed == (
        "2026-07-15T18:00:00+08:00",
        "2026-07-15T23:59:59+08:00",
    )


def test_format_duration_and_local_time_context() -> None:
    assert "分钟" in format_duration_zh(150)
    text = format_local_time_context(
        "2026-07-20T21:43:00+08:00",
        seconds_since_interaction=600,
    )
    assert "当前本地时间" in text
    assert "晚上" in text
    assert "距上次对话约 10 分钟" in text
    assert "客观间隔" in text
    assert "不要说成明显更短" in text
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
        expired_label="已过期的约定",
    )
    assert "已过期的约定" in expired
    assert "约1周前" in expired
    generic = annotate_with_relative_age(
        "旧近况",
        (now - timedelta(days=8)).isoformat(),
        now=now,
        expired=True,
    )
    assert "已失效" in generic


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
    assert "距上次对话约 15 分钟" in time_frag.content
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
