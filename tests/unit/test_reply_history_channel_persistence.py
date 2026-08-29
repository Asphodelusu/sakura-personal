"""主动回复历史 channel 持久化的最小化回归测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.llm.chat_reply import ChatSegment
from app.ui.pet_window import PetWindow


def _fake_record_history_window() -> tuple[object, list[dict[str, object]]]:
    calls: list[dict[str, object]] = []

    class Window(SimpleNamespace):
        pass

    window = Window()

    def record_history(
        self,
        role: str,
        content: str,
        translation: str = "",
        tone: str = "",
        portrait: str = "",
        channel: str = "",
        _debug: dict | None = None,
    ) -> int:
        calls.append(
            {
                "role": role,
                "content": content,
                "translation": translation,
                "tone": tone,
                "portrait": portrait,
                "channel": channel,
                "debug": _debug,
            },
        )
        return len(calls)

    window._record_history = record_history.__get__(window, Window)  # type: ignore[assignment]
    return window, calls


def _legacy_record_history_store_window_with_channel() -> tuple[object, list[dict[str, object]]]:
    calls: list[dict[str, object]] = []

    class LegacyStore:
        def append(
            self,
            role: str,
            content: str,
            translation: str = "",
            tone: str = "",
            portrait: str = "",
            channel: str = "",
            _debug: dict | None = None,
        ) -> int:
            calls.append(
                {
                    "role": role,
                    "content": content,
                    "translation": translation,
                    "tone": tone,
                    "portrait": portrait,
                    "channel": channel,
                    "debug": _debug,
                }
            )
            return len(calls)

    class Window(SimpleNamespace):
        pass

    window = Window(history_store=LegacyStore())
    window._record_history = PetWindow._record_history.__get__(window, Window)  # type: ignore[assignment]
    return window, calls


def _segments_for_channel_tests() -> list[ChatSegment]:
    return [
        ChatSegment("先说一句。", "中性", "", "站立待机"),
        ChatSegment("   ", "中性", "", "站立待机"),
        ChatSegment("再说一句。", "高兴", "", "思考停顿"),
    ]


@pytest.mark.parametrize("message_source,expected_channel", [
    ("proactive", "proactive"),
    ("relationship", "relationship"),
])
def test_assistant_reply_history_persists_distinct_autonomous_channels(
    message_source: str,
    expected_channel: str,
) -> None:
    window, calls = _fake_record_history_window()
    reply = SimpleNamespace(
        text="先说一句。\n再说一句。",
        segments=_segments_for_channel_tests(),
    )

    history_ids = PetWindow._record_assistant_reply_history(
        window,
        reply,
        message_source=message_source,
        _debug={"source": message_source},
    )
    assert history_ids == [1, 2]
    assert len(calls) == 2
    assert [entry["channel"] for entry in calls] == [expected_channel, expected_channel]
    assert calls[0]["debug"] == {"source": message_source}
    assert calls[1]["debug"] is None


def test_assistant_reply_history_keeps_default_channel_when_source_empty() -> None:
    window, calls = _fake_record_history_window()
    reply = SimpleNamespace(
        text="先说一句。\n再说一句。",
        segments=_segments_for_channel_tests(),
    )

    history_ids = PetWindow._record_assistant_reply_history(
        window,
        reply,
        message_source="",
        _debug={"source": "user"},
    )
    assert history_ids == [1, 2]
    assert len(calls) == 2
    assert calls[0]["channel"] == ""
    assert calls[1]["channel"] == ""


def test_record_assistant_reply_history_real_record_history_forwards_channel_to_history_store() -> None:
    window, calls = _legacy_record_history_store_window_with_channel()
    reply = SimpleNamespace(
        text="先说一句。\n再说一句。",
        segments=_segments_for_channel_tests(),
    )

    history_ids = PetWindow._record_assistant_reply_history(
        window,
        reply,
        message_source="proactive",
        _debug={"source": "proactive"},
    )
    assert history_ids == [1, 2]
    assert len(calls) == 2
    assert [entry["channel"] for entry in calls] == ["proactive", "proactive"]


class _ObserverAnchorDouble:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def record_proactive_exchange(
        self,
        *,
        source: str,
        history_ids: list[int],
        text: str,
        spoken_at_unix: float | None = None,
    ) -> bool:
        self.calls.append(
            {
                "source": source,
                "history_ids": list(history_ids),
                "text": text,
                "spoken_at_unix": spoken_at_unix,
            }
        )
        return True


def test_proactive_history_notifies_observer_once_after_all_ids() -> None:
    window, _calls = _fake_record_history_window()
    observer = _ObserverAnchorDouble()
    window._proactive_observer = observer
    reply = SimpleNamespace(
        text="先说一句。\n再说一句。",
        segments=_segments_for_channel_tests(),
    )

    history_ids = PetWindow._record_assistant_reply_history(
        window,
        reply,
        message_source="proactive",
        _debug={"source": "proactive"},
    )

    assert history_ids == [1, 2]
    assert len(observer.calls) == 1
    assert observer.calls[0]["source"] == "screen"
    assert observer.calls[0]["history_ids"] == [1, 2]
    assert observer.calls[0]["text"] == "先说一句。再说一句。"


def test_relationship_history_notifies_relationship_source() -> None:
    window, _calls = _fake_record_history_window()
    observer = _ObserverAnchorDouble()
    window._proactive_observer = observer
    reply = SimpleNamespace(
        text="先说一句。\n再说一句。",
        segments=_segments_for_channel_tests(),
    )

    PetWindow._record_assistant_reply_history(
        window,
        reply,
        message_source="relationship",
    )
    assert observer.calls[0]["source"] == "relationship"
    assert observer.calls[0]["history_ids"] == [1, 2]


def test_ordinary_reply_does_not_create_proactive_anchor() -> None:
    window, _calls = _fake_record_history_window()
    observer = _ObserverAnchorDouble()
    window._proactive_observer = observer
    reply = SimpleNamespace(
        text="先说一句。\n再说一句。",
        segments=_segments_for_channel_tests(),
    )
    PetWindow._record_assistant_reply_history(window, reply, message_source="")
    assert observer.calls == []


def test_zero_history_id_suppresses_anchor() -> None:
    window, _calls = _fake_record_history_window()
    observer = _ObserverAnchorDouble()
    window._proactive_observer = observer
    ids = iter([4, 0])

    def record_history(self, *_args, **_kwargs) -> int:
        return next(ids)

    window._record_history = record_history.__get__(window, type(window))
    reply = SimpleNamespace(
        text="先说一句。\n再说一句。",
        segments=_segments_for_channel_tests(),
    )
    PetWindow._record_assistant_reply_history(
        window, reply, message_source="proactive"
    )
    assert observer.calls == []


def test_missing_observer_during_history_write_is_harmless() -> None:
    window, _calls = _fake_record_history_window()
    window._proactive_observer = None
    reply = SimpleNamespace(
        text="先说一句。\n再说一句。",
        segments=_segments_for_channel_tests(),
    )
    history_ids = PetWindow._record_assistant_reply_history(
        window, reply, message_source="proactive"
    )
    assert history_ids == [1, 2]
