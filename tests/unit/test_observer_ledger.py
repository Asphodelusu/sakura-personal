"""Observer cost/effect ledger: one settlement record per evaluation attempt."""

from __future__ import annotations

import asyncio
import json
import os
import time
from unittest.mock import AsyncMock, patch

import pytest

from app.config.relationship_initiative import RelationshipInitiativeSettings
from app.perception.observer import FocusSnapshot, ProactiveConfig, ProactiveObserver
from app.perception.screen_capture import ScreenObservation
from app.perception.screen_reader import WindowText
from app.perception.sensory_impression import sensory_impression_store

_PID_OTHER = 10_001
assert _PID_OTHER != os.getpid()

_SECRET_VISUAL = "SECRET_VISUAL_SUMMARY"
_SECRET_SCREEN = "SECRET_SCREEN_TEXT"
_SECRET_REACTION = "SECRET_REACTION_HINT"
_SECRET_COMMENT = "私密对白コメント"
_SECRET_TRANSLATION = "秘密翻译正文"
_SECRET_REASON = "SECRET_REASON_BODY"
_SECRET_SUMMARY = "SECRET_SITUATIONAL_SUMMARY"

_PRIVATE_SAMPLES = (
    _SECRET_VISUAL,
    _SECRET_SCREEN,
    _SECRET_REACTION,
    _SECRET_COMMENT,
    _SECRET_TRANSLATION,
    _SECRET_REASON,
    _SECRET_SUMMARY,
)

_BODY_KEYS = {
    "body",
    "comment",
    "content",
    "messages",
    "on_screen_text",
    "prompt",
    "reaction_hint",
    "reason",
    "reply",
    "response",
    "system_prompt",
    "text",
    "translation",
    "visual_summary",
}

_VLM_JSON = json.dumps(
    {
        "visual_summary": _SECRET_VISUAL,
        "on_screen_text": _SECRET_SCREEN,
        "reaction_hint": _SECRET_REACTION,
        "suggested_interval": 480,
    },
    ensure_ascii=False,
)
_SPEAK_JSON = json.dumps(
    {
        "should_speak": True,
        "comment": _SECRET_COMMENT,
        "translation": _SECRET_TRANSLATION,
        "tone": "中性",
        "reason": _SECRET_REASON,
        "situational_summary": _SECRET_SUMMARY,
    },
    ensure_ascii=False,
)
_SILENT_JSON = json.dumps(
    {
        "should_speak": False,
        "comment": "",
        "translation": "",
        "tone": "",
        "reason": _SECRET_REASON,
        "situational_summary": _SECRET_SUMMARY,
    },
    ensure_ascii=False,
)
_EMPTY_PERCEPTION_JSON = json.dumps(
    {
        "visual_summary": "",
        "on_screen_text": "",
        "reaction_hint": "",
        "suggested_interval": 480,
    },
    ensure_ascii=False,
)
_EMPTY_COMMENT_JSON = json.dumps(
    {
        "should_speak": True,
        "comment": "",
        "translation": "",
        "tone": "中性",
        "reason": _SECRET_REASON,
        "situational_summary": _SECRET_SUMMARY,
    },
    ensure_ascii=False,
)
_VLM_USAGE = {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}
_DECISION_USAGE = {"prompt_tokens": 21, "completion_tokens": 7, "total_tokens": 28}


@pytest.fixture(autouse=True)
def _clear_sensory_impression():  # type: ignore[no-untyped-def]
    sensory_impression_store.clear()
    yield
    sensory_impression_store.clear()


def _json_response(content: str, usage: dict[str, int] | None = None) -> AsyncMock:
    payload: dict[str, object] = {
        "choices": [
            {
                "message": {"content": content},
                "finish_reason": "stop",
            }
        ]
    }
    if usage is not None:
        payload["usage"] = usage
    response = AsyncMock()
    response.raise_for_status = lambda: None
    response.json = lambda: payload
    return response


def _screen_observer() -> ProactiveObserver:
    observer = ProactiveObserver(
        api_base_url="https://vlm.example.com",
        api_key="vk",
        api_model="vision-m",
        chat_api_base_url="https://chat.example.com",
        chat_api_key="ck",
        chat_api_model="decision-m",
        config=ProactiveConfig(enabled=True, min_silence_after_user=0),
    )
    observer.capture.grab = lambda: ScreenObservation(
        ts="2026-01-01T00:00:00Z",
        width=8,
        height=8,
        image_b64="aaaa",
        monitor_index=1,
        dhash=0xABCD,
    )
    observer._get_window_text_for_eval = lambda: WindowText(is_accessible=False)
    observer.privacy.check_active_window = lambda: (False, "")
    return observer


def _relationship_observer() -> ProactiveObserver:
    settings = RelationshipInitiativeSettings(
        proactive_enabled=True,
        proactive_cooldown_seconds=3600,
        proactive_min_silence_seconds=300,
        desktop_idle_seconds=86_400,
    ).normalized()
    observer = ProactiveObserver(
        api_base_url="https://vlm.example.com",
        api_key="vk",
        api_model="vision-m",
        chat_api_base_url="https://chat.example.com",
        chat_api_key="ck",
        chat_api_model="decision-m",
        config=ProactiveConfig(enabled=False, min_silence_after_user=0),
        relationship=settings,
    )
    observer._last_user_at = 0.0
    observer._last_relationship_spoken_at = 0.0
    observer._last_relationship_silent_at = 0.0
    observer._relationship_silence_streak = 0
    return observer


def _wire_http(
    observer: ProactiveObserver,
    *,
    vlm_content: str | None = _VLM_JSON,
    vlm_usage: dict[str, int] | None = None,
    decision_content: str | None = _SPEAK_JSON,
    decision_usage: dict[str, int] | None = None,
    vlm_error: Exception | None = None,
) -> None:
    if vlm_content is not None or vlm_error is not None:
        observer._http = AsyncMock()
        if vlm_error is not None:
            observer._http.post = AsyncMock(side_effect=vlm_error)
        else:
            observer._http.post = AsyncMock(
                return_value=_json_response(vlm_content or "", vlm_usage)
            )
    if decision_content is not None:
        observer._chat_http = AsyncMock()
        observer._chat_http.post = AsyncMock(
            return_value=_json_response(decision_content, decision_usage)
        )


def _ledger_records(mock_debug_log) -> list[dict]:
    records: list[dict] = []
    for call in mock_debug_log.call_args_list:
        args = call.args
        if len(args) < 2:
            continue
        if args[0] != "ObserverLedger" or args[1] != "评估结算":
            continue
        data = args[2] if len(args) > 2 else call.kwargs.get("data")
        assert isinstance(data, dict)
        records.append(data)
    return records


def _assert_private_free(data: dict) -> None:
    blob = json.dumps(data, ensure_ascii=False)
    for sample in _PRIVATE_SAMPLES:
        assert sample not in blob

    def _walk(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                assert key not in _BODY_KEYS
                _walk(item)
        elif isinstance(value, list):
            for item in value:
                _walk(item)

    _walk(data)


def _run_screen(observer: ProactiveObserver, triggers: list[str] | None = None) -> list[dict]:
    with (
        patch("app.perception.observer.debug_log") as mock_log,
        patch("app.perception.observer.get_active_window_pid", return_value=_PID_OTHER),
        patch("app.perception.observer.get_active_window_title", return_value="Chrome"),
        patch("app.perception.observer.get_idle_seconds", return_value=0.0),
        patch(
            "app.perception.observer.get_active_window_process_name",
            return_value="chrome.exe",
        ),
    ):
        asyncio.run(observer._do_evaluation(triggers or ["timer"]))
        return _ledger_records(mock_log)


def test_screen_speak_settles_once_with_models_triggers_and_format() -> None:
    observer = _screen_observer()
    spoken: list = []
    observer.on_speak = lambda payload: spoken.append(payload)
    _wire_http(
        observer,
        vlm_usage=_VLM_USAGE,
        decision_usage=_DECISION_USAGE,
    )

    records = _run_screen(observer, ["timer", "window:A->B"])

    assert len(spoken) == 1
    assert spoken[0].source == "screen"
    assert len(records) == 1
    data = records[0]
    assert data["path"] == "screen"
    assert data["source"] == "screen"
    assert data["outcome"] == "speak"
    assert data["vlm_model"] == "vision-m"
    assert data["decision_model"] == "decision-m"
    assert data["triggers"] == ["timer", "window:A->B"]
    assert data["decision_format"] == "valid_json"
    assert data["vlm_usage"] == _VLM_USAGE
    assert data["decision_usage"] == _DECISION_USAGE
    assert isinstance(data["vlm_elapsed_ms"], int)
    assert isinstance(data["decision_elapsed_ms"], int)
    assert isinstance(data["total_elapsed_ms"], int)
    _assert_private_free(data)


def test_screen_silent_settles_valid_json_without_usage_keys() -> None:
    observer = _screen_observer()
    _wire_http(observer, decision_content=_SILENT_JSON)

    records = _run_screen(observer)

    assert len(records) == 1
    data = records[0]
    assert data["outcome"] == "silent"
    assert data["path"] == "screen"
    assert data["source"] == "screen"
    assert data["decision_format"] == "valid_json"
    assert "vlm_usage" not in data
    assert "decision_usage" not in data
    _assert_private_free(data)


def test_screen_capture_error_is_not_vlm_or_decision_error() -> None:
    observer = _screen_observer()
    observer.capture.grab = lambda: (_ for _ in ()).throw(RuntimeError("grab failed"))
    observer._chat_completion = AsyncMock(side_effect=AssertionError("vlm"))
    observer._post_speech_decision = AsyncMock(side_effect=AssertionError("decision"))

    records = _run_screen(observer)

    assert len(records) == 1
    data = records[0]
    assert data["outcome"] == "capture_error"
    assert data["path"] == "screen"
    assert data["source"] == "screen"
    assert "vlm_elapsed_ms" not in data
    assert "decision_elapsed_ms" not in data
    assert "decision_format" not in data
    _assert_private_free(data)


def test_screen_vlm_http_error_settles_vlm_error_once() -> None:
    observer = _screen_observer()
    _wire_http(observer, vlm_error=RuntimeError("vlm down"), decision_content=None)

    records = _run_screen(observer)

    assert len(records) == 1
    data = records[0]
    assert data["outcome"] == "vlm_error"
    assert data["path"] == "screen"
    assert data["vlm_model"] == "vision-m"
    assert "decision_format" not in data
    assert "vlm_usage" not in data
    assert "decision_elapsed_ms" not in data


def test_screen_vlm_parse_failure_settles_vlm_error_once() -> None:
    observer = _screen_observer()
    _wire_http(
        observer,
        vlm_content="not-json VLM output",
        vlm_usage=_VLM_USAGE,
        decision_content=None,
    )
    observer._post_speech_decision = AsyncMock(side_effect=AssertionError("decision"))

    records = _run_screen(observer)

    assert len(records) == 1
    data = records[0]
    assert data["outcome"] == "vlm_error"
    assert data["vlm_usage"] == _VLM_USAGE
    assert isinstance(data["vlm_elapsed_ms"], int)
    assert "decision_elapsed_ms" not in data
    assert "decision_format" not in data


def test_screen_decision_failure_settles_decision_error_after_vlm() -> None:
    observer = _screen_observer()
    _wire_http(
        observer,
        vlm_usage=_VLM_USAGE,
        decision_content="I should speak now.",
        decision_usage=_DECISION_USAGE,
    )

    records = _run_screen(observer)

    assert len(records) == 1
    data = records[0]
    assert data["outcome"] == "decision_error"
    assert data["vlm_usage"] == _VLM_USAGE
    assert data["decision_usage"] == _DECISION_USAGE
    assert data["decision_format"] == "rejected_invalid_output"
    assert isinstance(data["vlm_elapsed_ms"], int)
    assert isinstance(data["decision_elapsed_ms"], int)


def test_screen_dedup_skip_does_not_call_models() -> None:
    observer = _screen_observer()
    observer._last_frame_dhash = 0xABCD
    observer._chat_completion = AsyncMock(side_effect=AssertionError("vlm"))
    observer._post_speech_decision = AsyncMock(side_effect=AssertionError("decision"))

    records = _run_screen(observer, ["timer"])

    assert len(records) == 1
    data = records[0]
    assert data["outcome"] == "dedup_skip"
    assert data["path"] == "screen"
    assert data["source"] == "screen"
    assert data["triggers"] == ["timer"]
    assert "vlm_elapsed_ms" not in data
    assert "decision_elapsed_ms" not in data
    assert "decision_format" not in data


def test_screen_semantic_dedup_after_vlm_is_not_counted_as_silent() -> None:
    observer = _screen_observer()
    observer._last_eval_window_title = "Chrome"
    observer._last_visual_summary = _SECRET_VISUAL
    _wire_http(observer, vlm_usage=_VLM_USAGE)
    observer._post_speech_decision = AsyncMock(
        side_effect=AssertionError("semantic dedup must skip decision model")
    )

    records = _run_screen(observer, ["timer"])

    assert len(records) == 1
    data = records[0]
    assert data["outcome"] == "dedup_skip"
    assert data["vlm_usage"] == _VLM_USAGE
    assert "decision_elapsed_ms" not in data
    observer._post_speech_decision.assert_not_awaited()


def test_screen_own_process_aborts_as_preflight_skip() -> None:
    observer = _screen_observer()
    observer._focus_current = FocusSnapshot(
        hwnd=1, process="sakura.exe", title="Sakura", pid=os.getpid()
    )
    observer.capture.grab = lambda: (_ for _ in ()).throw(AssertionError("capture"))
    observer._chat_completion = AsyncMock(side_effect=AssertionError("vlm"))

    records = _run_screen(observer)

    assert len(records) == 1
    data = records[0]
    assert data["outcome"] == "preflight_skip"
    assert data["path"] == "screen"
    assert data["triggers"] == ["timer"]
    assert "vlm_elapsed_ms" not in data
    assert "decision_elapsed_ms" not in data


def test_screen_foreground_pid_self_is_preflight_skip() -> None:
    observer = _screen_observer()
    observer.capture.grab = lambda: (_ for _ in ()).throw(AssertionError("capture"))
    observer._chat_completion = AsyncMock(side_effect=AssertionError("vlm"))

    with (
        patch("app.perception.observer.debug_log") as mock_log,
        patch("app.perception.observer.get_active_window_pid", return_value=os.getpid()),
        patch("app.perception.observer.get_active_window_title", return_value="Sakura"),
        patch("app.perception.observer.get_idle_seconds", return_value=0.0),
        patch(
            "app.perception.observer.get_active_window_process_name",
            return_value="sakura.exe",
        ),
    ):
        asyncio.run(observer._do_evaluation(["timer"]))
        records = _ledger_records(mock_log)

    assert len(records) == 1
    assert records[0]["outcome"] == "preflight_skip"
    assert "vlm_elapsed_ms" not in records[0]


def test_screen_privacy_block_aborts_as_preflight_skip() -> None:
    observer = _screen_observer()
    observer.privacy.check_active_window = lambda: (True, "secret.exe")
    observer.capture.grab = lambda: (_ for _ in ()).throw(AssertionError("capture"))
    observer._chat_completion = AsyncMock(side_effect=AssertionError("vlm"))

    records = _run_screen(observer)

    assert len(records) == 1
    data = records[0]
    assert data["outcome"] == "preflight_skip"
    assert data["path"] == "screen"
    assert "vlm_elapsed_ms" not in data
    assert "decision_elapsed_ms" not in data


def test_screen_empty_perception_is_not_silent_or_vlm_error() -> None:
    observer = _screen_observer()
    _wire_http(
        observer,
        vlm_content=_EMPTY_PERCEPTION_JSON,
        vlm_usage=_VLM_USAGE,
        decision_content=None,
    )
    observer._post_speech_decision = AsyncMock(side_effect=AssertionError("decision"))

    records = _run_screen(observer)

    assert len(records) == 1
    data = records[0]
    assert data["outcome"] == "empty_perception"
    assert data["vlm_usage"] == _VLM_USAGE
    assert isinstance(data["vlm_elapsed_ms"], int)
    assert "decision_elapsed_ms" not in data
    assert "decision_format" not in data
    _assert_private_free(data)


def test_screen_should_speak_without_comment_stays_silent() -> None:
    observer = _screen_observer()
    spoken: list = []
    observer.on_speak = lambda payload: spoken.append(payload)
    _wire_http(observer, decision_content=_EMPTY_COMMENT_JSON)

    records = _run_screen(observer)

    assert spoken == []
    assert len(records) == 1
    data = records[0]
    assert data["outcome"] == "silent"
    assert data["decision_format"] == "valid_json"
    _assert_private_free(data)


def test_relationship_speak_settles_once() -> None:
    observer = _relationship_observer()
    spoken: list = []
    observer.on_speak = lambda payload: spoken.append(payload)
    _wire_http(observer, vlm_content=None, decision_usage=_DECISION_USAGE)

    with patch("app.perception.observer.debug_log") as mock_log:
        asyncio.run(observer._do_relationship_evaluation())
        records = _ledger_records(mock_log)

    assert len(spoken) == 1
    assert spoken[0].source == "relationship"
    assert len(records) == 1
    data = records[0]
    assert data["path"] == "relationship"
    assert data["source"] == "relationship"
    assert data["outcome"] == "speak"
    assert data["decision_model"] == "decision-m"
    assert "vlm_model" not in data
    assert "triggers" not in data
    assert data["decision_format"] == "valid_json"
    assert data["decision_usage"] == _DECISION_USAGE
    assert "vlm_usage" not in data
    assert isinstance(data["decision_elapsed_ms"], int)
    assert isinstance(data["total_elapsed_ms"], int)
    _assert_private_free(data)


def test_relationship_silent_settles_once() -> None:
    observer = _relationship_observer()
    _wire_http(observer, vlm_content=None, decision_content=_SILENT_JSON)

    with patch("app.perception.observer.debug_log") as mock_log:
        asyncio.run(observer._do_relationship_evaluation())
        records = _ledger_records(mock_log)

    assert len(records) == 1
    data = records[0]
    assert data["outcome"] == "silent"
    assert data["path"] == "relationship"
    assert data["source"] == "relationship"
    assert data["decision_format"] == "valid_json"
    _assert_private_free(data)


def test_relationship_should_speak_without_comment_stays_silent() -> None:
    observer = _relationship_observer()
    spoken: list = []
    observer.on_speak = lambda payload: spoken.append(payload)
    _wire_http(observer, vlm_content=None, decision_content=_EMPTY_COMMENT_JSON)

    with patch("app.perception.observer.debug_log") as mock_log:
        asyncio.run(observer._do_relationship_evaluation())
        records = _ledger_records(mock_log)

    assert spoken == []
    assert len(records) == 1
    assert records[0]["outcome"] == "silent"
    assert records[0]["decision_format"] == "valid_json"
    _assert_private_free(records[0])


def test_relationship_stale_cancel_settles_once_without_speaking() -> None:
    observer = _relationship_observer()
    spoken: list = []
    observer.on_speak = lambda payload: spoken.append(payload)
    observer._relationship_generation = 1

    async def _decide() -> dict[str, object]:
        observer.bump_relationship_generation()
        return {
            "should_speak": True,
            "comment": _SECRET_COMMENT,
            "reason": _SECRET_REASON,
        }

    observer._decide_relationship_speech = _decide
    with patch("app.perception.observer.debug_log") as mock_log:
        asyncio.run(observer._do_relationship_evaluation())
        records = _ledger_records(mock_log)

    assert spoken == []
    assert len(records) == 1
    assert records[0]["outcome"] == "stale_cancel"
    assert records[0]["path"] == "relationship"
    _assert_private_free(records[0])


def test_two_evaluations_each_settle_exactly_once() -> None:
    observer = _screen_observer()
    _wire_http(observer, decision_content=_SILENT_JSON)
    first = _run_screen(observer)
    observer._last_frame_dhash = None
    second = _run_screen(observer)
    assert len(first) == 1
    assert len(second) == 1
    assert first[0]["outcome"] == "silent"
    assert second[0]["outcome"] == "dedup_skip"


def test_busy_and_desktop_idle_ticks_do_not_ledger() -> None:
    busy = _relationship_observer()
    busy.config.enabled = True
    busy._is_busy = lambda: "worker_thread"
    idle = _relationship_observer()
    idle._last_user_at = 0.0
    now = 10_000.0
    idle._last_user_at = now - 400

    with (
        patch("app.perception.observer.debug_log") as mock_log,
        patch("app.perception.observer.get_idle_seconds", return_value=1_200.0),
    ):
        asyncio.run(busy._dispatch_proactive_tick(now))
        idle.relationship = RelationshipInitiativeSettings(
            proactive_enabled=True,
            desktop_idle_seconds=900,
        ).normalized()
        asyncio.run(idle._dispatch_proactive_tick(now))
        assert _ledger_records(mock_log) == []


def test_ledger_denylist_rejects_body_fields_and_private_samples() -> None:
    observer = _screen_observer()
    _wire_http(observer, vlm_usage=_VLM_USAGE, decision_usage=_DECISION_USAGE)
    records = _run_screen(observer)
    assert len(records) == 1
    _assert_private_free(records[0])


def test_exchange_context_record_stays_private_during_screen_eval() -> None:
    observer = _screen_observer()
    observer.set_history_entries_after_provider(lambda _after, _limit: [])
    observer.record_proactive_exchange(
        source="screen",
        history_ids=[10],
        text=_SECRET_COMMENT,
        spoken_at_unix=time.time(),
    )
    _wire_http(observer, decision_content=_SILENT_JSON)
    with (
        patch("app.perception.observer.debug_log") as mock_log,
        patch("app.perception.observer.get_active_window_pid", return_value=_PID_OTHER),
        patch("app.perception.observer.get_active_window_title", return_value="Chrome"),
        patch("app.perception.observer.get_idle_seconds", return_value=0.0),
        patch(
            "app.perception.observer.get_active_window_process_name",
            return_value="chrome.exe",
        ),
    ):
        asyncio.run(observer._do_evaluation(["timer"]))
        records = [
            call.args[2]
            for call in mock_log.call_args_list
            if len(call.args) >= 3
            and call.args[0] == "ObserverLedger"
            and call.args[1] == "交流上下文"
        ]
    assert len(records) == 1
    _assert_private_free(records[0])
    assert records[0]["view_count"] == 1
    assert records[0]["exchanges"][0]["history_end_id"] == 10
