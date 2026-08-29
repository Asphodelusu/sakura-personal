"""Observer 近期对话：时间/渠道/来源，以及分段折叠后的新旧事实。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.perception.observer import (
    ObserverHistoryLine,
    format_observer_recent_history,
    latest_ordinary_chat_unix,
)
from app.perception.sensory_impression import SensoryImpressionStore


TZ = timezone(timedelta(hours=8))


def _at(hour: int, minute: int) -> str:
    return datetime(2026, 8, 29, hour, minute, 0, tzinfo=TZ).isoformat(timespec="seconds")


def _line(
    role: str,
    content: str,
    *,
    created_at: str,
    channel: str = "",
) -> ObserverHistoryLine:
    return ObserverHistoryLine(
        role=role,
        content=content,
        created_at=created_at,
        channel=channel,
    )


def test_history_format_keeps_role_time_and_channel() -> None:
    now = datetime(2026, 8, 29, 18, 6, 0, tzinfo=TZ)
    text = format_observer_recent_history(
        [
            _line("user", "晚上吃亲子丼吧", created_at=_at(17, 2)),
            _line(
                "assistant",
                "好，等下一起吃亲子丼",
                created_at=_at(17, 2),
                channel="",
            ),
            _line("user", "不是吃完了吗", created_at=_at(17, 58)),
            _line(
                "assistant",
                "对，我们一起吃过了",
                created_at=_at(18, 4),
                channel="",
            ),
            _line(
                "assistant",
                "要不要出门走走",
                created_at=_at(18, 5),
                channel="relationship",
            ),
        ],
        now=now,
        max_turns=6,
    )

    assert "[最近の会話]" in text
    assert "我说的" in text
    assert "她自己的" in text
    assert "她自己的·关系主动" in text
    assert "17:02" in text
    assert "17:58" in text
    assert "18:04" in text
    plan_at = text.find("等下一起吃亲子丼")
    done_at = text.find("一起吃过了")
    correct_at = text.find("不是吃完了吗")
    assert plan_at != -1 and done_at != -1 and correct_at != -1
    assert plan_at < correct_at < done_at


def test_multi_segment_assistant_reply_does_not_crowd_out_later_correction() -> None:
    now = datetime(2026, 8, 29, 18, 6, 0, tzinfo=TZ)
    confirmation_parts = [
        "嗯，",
        "亲子丼已经吃完了。",
        "刚刚那碗很热。",
        "你还记得蛋汁吧。",
        "我们是一起吃的。",
        "不是还没吃。",
    ]
    entries = [
        _line("user", "晚上吃亲子丼吧", created_at=_at(17, 2)),
        _line("assistant", "好，等下一起去吃", created_at=_at(17, 2)),
        _line("user", "不是吃完了吗", created_at=_at(17, 58)),
    ]
    for part in confirmation_parts:
        entries.append(_line("assistant", part, created_at=_at(18, 4)))

    text = format_observer_recent_history(entries, now=now, max_turns=6)

    assert "不是吃完了吗" in text
    assert "一起去吃" in text
    assert text.count("[她自己的]") == 2
    assert "不是还没吃" in text


def test_separate_assistant_turns_with_same_channel_keep_their_own_time() -> None:
    now = datetime(2026, 8, 29, 18, 6, 0, tzinfo=TZ)
    text = format_observer_recent_history(
        [
            _line("assistant", "第一次主动说的话", created_at=_at(17, 2), channel="proactive"),
            _line("assistant", "第二次主动说的话", created_at=_at(18, 5), channel="proactive"),
        ],
        now=now,
        max_turns=6,
    )

    assert text.count("[她自己的·主动]") == 2
    assert "17:02" in text
    assert "18:05" in text


def test_later_completion_outranks_older_plan_in_oyakodon_chain() -> None:
    now = datetime(2026, 8, 29, 18, 6, 0, tzinfo=TZ)
    text = format_observer_recent_history(
        [
            _line("user", "去吃亲子丼", created_at=_at(17, 2)),
            _line("assistant", "刚才约好的亲子丼，等下一起吃", created_at=_at(17, 2)),
            _line("user", "吃完了", created_at=_at(17, 40)),
            _line("assistant", "一起吃了", created_at=_at(17, 41)),
            _line("user", "不是吃完了吗", created_at=_at(17, 58)),
            _line("assistant", "对，一起吃了", created_at=_at(18, 4)),
        ],
        now=now,
        max_turns=6,
    )
    latest_block = text.split("\n")[-1]
    assert "一起吃了" in latest_block
    assert "等下一起吃" not in latest_block
    assert latest_ordinary_chat_unix(
        [
            _line("user", "不是吃完了吗", created_at=_at(17, 58)),
            _line("assistant", "对，一起吃了", created_at=_at(18, 4)),
            _line("assistant", "催促", created_at=_at(18, 5), channel="proactive"),
        ]
    ) == datetime(2026, 8, 29, 18, 4, 0, tzinfo=TZ).timestamp()


def test_stale_sensory_impression_is_not_injected_after_newer_chat_facts() -> None:
    store = SensoryImpressionStore(ttl_seconds=3600.0)
    impression_unix = datetime(2026, 8, 29, 17, 10, 0, tzinfo=TZ).timestamp()
    chat_unix = datetime(2026, 8, 29, 18, 4, 0, tzinfo=TZ).timestamp()
    store.update(
        "相手はまだ親子丼を食べに行く予定。対話の既知：刚才约好的亲子丼。",
        now=100.0,
        wall_unix=impression_unix,
    )
    assert store.get_for_observer(now=200.0)
    assert store.get_for_observer(now=200.0, chat_facts_unix=chat_unix) == ""
    later_impression = datetime(2026, 8, 29, 18, 5, 30, tzinfo=TZ).timestamp()
    store.update(
        "相手はコードを見ている。",
        now=300.0,
        wall_unix=later_impression,
    )
    assert "コード" in store.get_for_observer(now=400.0, chat_facts_unix=chat_unix)
