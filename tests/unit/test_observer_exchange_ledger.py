"""短时主动交流账本：确定性状态推导，不分类接受/拒绝。"""

from __future__ import annotations

from app.perception.observer import (
    ObserverHistoryLine,
    ProactiveExchange,
    derive_proactive_exchange_view,
)


def _line(
    entry_id: int,
    role: str,
    content: str,
    *,
    channel: str = "",
    created_at: str = "2026-08-29T20:16:00+08:00",
) -> ObserverHistoryLine:
    return ObserverHistoryLine(
        role=role,
        content=content,
        created_at=created_at,
        channel=channel,
        id=entry_id,
    )


def _exchange() -> ProactiveExchange:
    return ProactiveExchange(
        source="screen",
        history_start_id=10,
        history_end_id=12,
        spoken_at_unix=1000.0,
        text="明日の昼、どうする？",
    )


def test_no_later_user_row_is_awaiting_reply() -> None:
    view = derive_proactive_exchange_view(
        _exchange(),
        [_line(13, "assistant", "还要不要出门", channel="relationship")],
        now_unix=1100.0,
    )
    assert view.state == "awaiting_reply"
    assert view.first_user_reply is None
    assert view.first_ordinary_assistant_followup is None


def test_any_later_user_row_is_engaged_without_settlement_label() -> None:
    reply = _line(14, "user", "听到了，会不会一起吃还是看情况吧……")
    followup = _line(15, "assistant", "作るなら教えて、私の分も少し多めにお願い。")
    view = derive_proactive_exchange_view(
        _exchange(),
        [
            _line(13, "assistant", "催一下", channel="proactive"),
            reply,
            followup,
        ],
        now_unix=1100.0,
    )
    assert view.state == "engaged"
    assert view.first_user_reply == reply
    assert view.first_ordinary_assistant_followup == followup
    assert view.first_user_reply is not None
    assert view.first_user_reply.content == "听到了，会不会一起吃还是看情况吧……"
    assert "accepted" not in view.state
    assert "rejected" not in view.state
    assert "settled" not in view.state


def test_question_or_unrelated_user_text_is_still_engaged() -> None:
    question = _line(16, "user", "你刚才说的是哪一天？")
    view = derive_proactive_exchange_view(
        _exchange(),
        [question],
        now_unix=1100.0,
    )
    assert view.state == "engaged"
    assert view.first_user_reply == question

    unrelated = _line(17, "user", "先把这段代码看完")
    view = derive_proactive_exchange_view(_exchange(), [unrelated], now_unix=1100.0)
    assert view.state == "engaged"
    assert view.first_user_reply == unrelated


def test_proactive_and_relationship_rows_are_not_ordinary_followup() -> None:
    reply = _line(14, "user", "晚点再说")
    view = derive_proactive_exchange_view(
        _exchange(),
        [
            reply,
            _line(15, "assistant", "那我再问一次", channel="proactive"),
            _line(16, "assistant", "要不要靠过来", channel="relationship"),
        ],
        now_unix=1100.0,
    )
    assert view.state == "engaged"
    assert view.first_ordinary_assistant_followup is None


def test_ordinary_followup_keeps_all_segments_from_the_same_turn() -> None:
    reply = _line(14, "user", "不是已经定下来了吗？")
    first = _line(
        15,
        "assistant",
        "……そうだったっけ。",
        created_at="2026-08-29T20:28:11+08:00",
    )
    second = _line(
        16,
        "assistant",
        "じゃあ、起きたら教えてね。",
        created_at="2026-08-29T20:28:11+08:00",
    )
    view = derive_proactive_exchange_view(
        _exchange(),
        [reply, first, second],
        now_unix=1100.0,
    )
    assert view.first_ordinary_assistant_followup is not None
    assert view.first_ordinary_assistant_followup.content == (
        "……そうだったっけ。じゃあ、起きたら教えてね。"
    )
    assert view.first_ordinary_assistant_followup.id == 16


def test_ttl_expiry_marks_expired() -> None:
    view = derive_proactive_exchange_view(
        _exchange(),
        [_line(14, "user", "嗯")],
        now_unix=1000.0 + 1200.0 + 1.0,
    )
    assert view.state == "expired"


def _observer():
    from app.perception.observer import ProactiveConfig, ProactiveObserver

    return ProactiveObserver(
        api_base_url="https://example.com",
        api_key="x",
        api_model="m",
        config=ProactiveConfig(enabled=False),
    )


def test_new_observer_starts_with_empty_exchange_ledger() -> None:
    observer = _observer()
    assert observer._current_exchange_views(now_unix=1100.0) == []


def test_valid_persisted_ids_create_one_anchor() -> None:
    observer = _observer()
    observer.set_history_entries_after_provider(lambda _after, _limit: [])
    created = observer.record_proactive_exchange(
        source="screen",
        history_ids=[10, 12],
        text="明日の昼、どうする？",
        spoken_at_unix=1000.0,
    )
    assert created is True
    views = observer._current_exchange_views(now_unix=1100.0)
    assert len(views) == 1
    assert views[0].exchange.source == "screen"
    assert views[0].exchange.history_start_id == 10
    assert views[0].exchange.history_end_id == 12
    assert views[0].state == "awaiting_reply"


def test_invalid_or_partial_ids_create_no_anchor() -> None:
    observer = _observer()
    observer.set_history_entries_after_provider(lambda _after, _limit: [])
    assert observer.record_proactive_exchange(source="screen", history_ids=[], text="x") is False
    assert observer.record_proactive_exchange(source="screen", history_ids=[0, 2], text="x") is False
    assert observer.record_proactive_exchange(source="screen", history_ids=[3, -1], text="x") is False
    assert observer._current_exchange_views(now_unix=1100.0) == []


def test_screen_and_relationship_sources_stay_distinct() -> None:
    observer = _observer()
    observer.set_history_entries_after_provider(lambda _after, _limit: [])
    observer.record_proactive_exchange(
        source="screen", history_ids=[1], text="screen-q", spoken_at_unix=1000.0
    )
    observer.record_proactive_exchange(
        source="relationship", history_ids=[2], text="rel-q", spoken_at_unix=1001.0
    )
    sources = [view.exchange.source for view in observer._current_exchange_views(now_unix=1100.0)]
    assert sources == ["relationship", "screen"]


def test_ledger_keeps_only_five_newest_anchors() -> None:
    observer = _observer()
    observer.set_history_entries_after_provider(lambda _after, _limit: [])
    for index in range(6):
        observer.record_proactive_exchange(
            source="screen",
            history_ids=[10 + index],
            text=f"q-{index}",
            spoken_at_unix=1000.0 + index,
        )
    views = observer._current_exchange_views(now_unix=1100.0)
    assert len(views) == 5
    assert [view.exchange.history_end_id for view in views] == [15, 14, 13, 12, 11]


def test_format_engaged_exchange_never_claims_unanswered_or_settled() -> None:
    from app.perception.observer import format_proactive_exchange_context

    reply = _line(14, "user", "听到了，会不会一起吃还是看情况吧……")
    followup = _line(15, "assistant", "作るなら教えて、私の分も少し多めにお願い。")
    view = derive_proactive_exchange_view(
        _exchange(),
        [reply, followup],
        now_unix=1100.0,
    )
    text = format_proactive_exchange_context([view])
    assert "[近期主动交流 · 已得到回应]" in text
    assert "明日の昼、どうする？" in text
    assert "听到了，会不会一起吃还是看情况吧……" in text
    assert "作るなら教えて" in text
    lowered = text.lower()
    for banned in ("unanswered", "agreed", "accepted", "settled", "未回应", "没有回应"):
        assert banned not in text and banned not in lowered


def test_decision_context_keeps_exchange_after_six_unrelated_turns() -> None:
    from datetime import datetime, timedelta, timezone

    from app.perception.observer import (
        ObservationPacket,
        format_observer_recent_history,
        format_proactive_exchange_context,
    )

    tz = timezone(timedelta(hours=8))
    recent_lines = []
    for index in range(7, 13):
        hour = 20
        minute = 20 + index
        stamp = datetime(2026, 8, 29, hour, minute, tzinfo=tz).isoformat(timespec="seconds")
        recent_lines.append(_line(20 + index, "user", f"unrelated-user-{index}", created_at=stamp))
        recent_lines.append(
            _line(40 + index, "assistant", f"unrelated-assistant-{index}", created_at=stamp)
        )
    recent = format_observer_recent_history(recent_lines, max_turns=6)
    assert "明日の昼、どうする？" not in recent
    assert "听到了，会不会一起吃还是看情况吧……" not in recent

    observer = _observer()
    observer.set_recent_history_provider(lambda: recent)
    observer.set_history_entries_after_provider(
        lambda _after, _limit: [
            _line(14, "user", "听到了，会不会一起吃还是看情况吧……"),
            _line(15, "assistant", "作るなら教えて、私の分も少し多めにお願い。"),
        ]
    )
    observer.record_proactive_exchange(
        source="screen",
        history_ids=[10, 12],
        text="明日の昼、どうする？",
        spoken_at_unix=1000.0,
    )
    observer._last_spoken_text = "昼ごはん、どうする？"
    packet = ObservationPacket(window_title="Code", visual_summary="コードを見ている")
    text = observer._build_speech_decision_user_content(packet, now_unix=1100.0)
    assert "unrelated-user-12" in text
    assert "[近期主动交流 · 已得到回应]" in text
    assert "明日の昼、どうする？" in text
    assert "听到了，会不会一起吃还是看情况吧……" in text
    assert "作るなら教えて" in text
    assert "[自分の直前の発話]" not in text
    assert "未回应" not in text
    assert format_proactive_exchange_context(observer._current_exchange_views(now_unix=1100.0))


def test_exchange_context_diagnostics_are_content_free() -> None:
    import json
    from unittest.mock import patch

    from app.perception.observer import ObservationPacket

    secret_proactive = "SECRET_PROACTIVE_LUNCH_Q"
    secret_reply = "SECRET_USER_REPLY_BODY"
    secret_follow = "SECRET_ASSISTANT_FOLLOWUP"
    secret_visual = "SECRET_VISUAL_SUMMARY"
    secret_reaction = "SECRET_REACTION_HINT"
    observer = _observer()
    observer.set_history_entries_after_provider(
        lambda _after, _limit: [
            _line(14, "user", secret_reply),
            _line(15, "assistant", secret_follow),
        ]
    )
    observer.record_proactive_exchange(
        source="screen",
        history_ids=[10, 12],
        text=secret_proactive,
        spoken_at_unix=1000.0,
    )
    packet = ObservationPacket(
        window_title="Code",
        visual_summary=secret_visual,
        reaction_hint=secret_reaction,
    )
    with patch("app.perception.observer.debug_log") as mock_log:
        observer._build_speech_decision_user_content(packet, now_unix=1100.0)
        records = [
            call.args[2]
            for call in mock_log.call_args_list
            if len(call.args) >= 3
            and call.args[0] == "ObserverLedger"
            and call.args[1] == "交流上下文"
        ]
    assert len(records) == 1
    data = records[0]
    assert data["view_count"] == 1
    assert data["state_counts"]["engaged"] == 1
    assert data["exchanges"][0]["source"] == "screen"
    assert data["exchanges"][0]["history_start_id"] == 10
    assert data["exchanges"][0]["history_end_id"] == 12
    assert data["exchanges"][0]["state"] == "engaged"
    assert isinstance(data["exchanges"][0]["age_s"], int)
    assert isinstance(data["elapsed_ms"], int)
    blob = json.dumps(data, ensure_ascii=False)
    for secret in (
        secret_proactive,
        secret_reply,
        secret_follow,
        secret_visual,
        secret_reaction,
    ):
        assert secret not in blob
    for key in ("text", "comment", "prompt", "reaction_hint", "visual_summary"):
        assert key not in data
        assert all(key not in item for item in data["exchanges"])


def test_provider_failure_drops_anchors_instead_of_awaiting_reply() -> None:
    observer = _observer()

    def boom(_after: int, _limit: int):
        raise OSError("history unavailable")

    observer.set_history_entries_after_provider(boom)
    assert observer.record_proactive_exchange(
        source="screen", history_ids=[10], text="明日の昼、どうする？", spoken_at_unix=1000.0
    )
    assert observer._current_exchange_views(now_unix=1100.0) == []
    observer.set_history_entries_after_provider(lambda _after, _limit: [])
    assert observer._current_exchange_views(now_unix=1100.0) == []


def test_each_anchor_reads_its_own_bounded_history_window() -> None:
    observer = _observer()
    observer.record_proactive_exchange(
        source="screen",
        history_ids=[10],
        text="old question",
        spoken_at_unix=1000.0,
    )
    observer.record_proactive_exchange(
        source="screen",
        history_ids=[200],
        text="new question",
        spoken_at_unix=1001.0,
    )

    def history_after(after_id: int, _limit: int):
        if after_id == 10:
            return [_line(entry_id, "assistant", f"row-{entry_id}") for entry_id in range(11, 111)]
        if after_id == 200:
            return [_line(201, "user", "new answer")]
        return []

    observer.set_history_entries_after_provider(history_after)
    views = observer._current_exchange_views(now_unix=1100.0)
    states = {view.exchange.history_end_id: view.state for view in views}
    assert states[200] == "engaged"


def test_pruning_preserves_anchor_added_while_history_is_read() -> None:
    observer = _observer()
    observer.record_proactive_exchange(
        source="screen",
        history_ids=[10],
        text="old question",
        spoken_at_unix=1000.0,
    )
    added = False

    def history_after(after_id: int, _limit: int):
        nonlocal added
        if not added:
            added = True
            observer.record_proactive_exchange(
                source="relationship",
                history_ids=[30],
                text="new concurrent question",
                spoken_at_unix=1001.0,
            )
        return [_line(11, "user", "old answer")] if after_id == 10 else []

    observer.set_history_entries_after_provider(history_after)
    observer._current_exchange_views(now_unix=1100.0)
    observer.set_history_entries_after_provider(lambda _after, _limit: [])
    remaining = observer._current_exchange_views(now_unix=1100.0)
    assert {view.exchange.history_end_id for view in remaining} == {10, 30}
