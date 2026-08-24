import asyncio
from unittest.mock import AsyncMock, patch

from app.config.relationship_initiative import RelationshipInitiativeSettings
from app.perception.observer import ProactiveConfig, ProactiveObserver


def _obs(*, busy: str = "", **rel: object) -> ProactiveObserver:
    settings = RelationshipInitiativeSettings(
        proactive_enabled=bool(rel.get("proactive_enabled", True)),
        proactive_cooldown_seconds=int(rel.get("cooldown", 3600)),
        proactive_min_silence_seconds=int(rel.get("silence", 300)),
    ).normalized()
    observer = ProactiveObserver(
        api_base_url="https://example.com",
        api_key="x",
        api_model="m",
        config=ProactiveConfig(
            enabled=True,
            timer_seconds=9999,
            cooldown_seconds=600,
            min_silence_after_user=10,
            content_check_interval=9999,
            idle_threshold_seconds=99999,
            poll_interval=5,
        ),
        relationship=settings,
        is_busy=lambda: busy,
    )
    observer._last_user_at = 0.0
    observer._last_eval_at = 0.0
    observer._last_proactive_at = 0.0
    observer._last_silent_eval_at = 0.0
    observer._last_relationship_spoken_at = 0.0
    observer._last_relationship_silent_at = 0.0
    return observer


def test_bare_observer_does_not_collect_relationship_timer() -> None:
    observer = ProactiveObserver(
        api_base_url="https://example.com",
        api_key="x",
        api_model="m",
        config=ProactiveConfig(enabled=True, min_silence_after_user=0, timer_seconds=9999),
    )
    observer._last_user_at = 0.0
    assert observer._relationship_gate_reason(1000.0, "") == "disabled"


def test_gates_cover_disabled_busy_silence_cooldown_continuation() -> None:
    now = 10_000.0
    disabled = _obs(proactive_enabled=False)
    assert disabled._relationship_gate_reason(now, "") == "disabled"
    busy = _obs(busy="worker_thread")
    busy._last_user_at = now - 400
    assert busy._relationship_gate_reason(now, "worker_thread") == "busy"
    continuation = _obs(busy="rhythm_focus")
    continuation._last_user_at = now - 400
    assert continuation._relationship_gate_reason(now, "rhythm_focus") == "continuation"
    silent = _obs()
    silent._last_user_at = now - 120
    assert silent._relationship_gate_reason(now, "") == "silence"
    cooling = _obs()
    cooling._last_user_at = now - 400
    cooling._last_relationship_spoken_at = now - 10
    assert cooling._relationship_gate_reason(now, "") == "cooldown"
    ready = _obs()
    ready._last_user_at = now - 400
    assert ready._relationship_gate_reason(now, "") == "eligible"


def test_screen_cooldown_does_not_block_relationship_and_vice_versa() -> None:
    now = 10_000.0
    observer = _obs()
    observer._last_user_at = now - 400
    observer._last_proactive_at = now - 1
    observer._last_silent_eval_at = now - 1
    assert observer._relationship_gate_reason(now, "") == "eligible"
    observer._last_relationship_spoken_at = now - 1
    observer._last_proactive_at = 0.0
    observer._last_silent_eval_at = 0.0
    assert observer._relationship_gate_reason(now, "") == "cooldown"


def test_relationship_eval_does_not_capture_or_call_vlm() -> None:
    observer = _obs()
    observer._last_user_at = 0.0
    observer.capture.grab = lambda: (_ for _ in ()).throw(AssertionError("screenshot"))
    observer._get_window_text_for_eval = lambda: (_ for _ in ()).throw(AssertionError("uia"))
    observer._chat_completion = AsyncMock(side_effect=AssertionError("vlm"))
    observer._decide_relationship_speech = AsyncMock(return_value={"should_speak": False, "reason": "静かに"})
    asyncio.run(observer._do_relationship_evaluation())
    observer._decide_relationship_speech.assert_awaited()
    observer._chat_completion.assert_not_called()


def test_screen_trigger_wins_same_tick_and_suppresses_relationship_eval() -> None:
    observer = _obs()
    now = 10_000.0
    observer._last_user_at = now - 400
    observer._ready_focus_trigger = "window:A->B"
    observer._do_evaluation = AsyncMock()
    observer._do_relationship_evaluation = AsyncMock()
    with (
        patch("app.perception.observer.get_active_window_pid", return_value=10_001),
        patch("app.perception.observer.time.monotonic", return_value=now),
    ):
        asyncio.run(observer._dispatch_proactive_tick(now))
    observer._do_evaluation.assert_awaited()
    observer._do_relationship_evaluation.assert_not_called()
    assert observer._last_relationship_silent_at == now
    assert observer._relationship_gate_reason(now + 1, "") == "cooldown"


def test_screen_evaluation_failure_clears_relationship_motive() -> None:
    observer = _obs()
    now = 10_000.0
    observer._last_user_at = now - 400
    observer._ready_focus_trigger = "window:A->B"
    observer._do_evaluation = AsyncMock(side_effect=RuntimeError("boom"))

    with (
        patch("app.perception.observer.get_active_window_pid", return_value=10_001),
        patch("app.perception.observer.time.monotonic", return_value=now),
    ):
        try:
            asyncio.run(observer._dispatch_proactive_tick(now))
        except RuntimeError:
            pass

    assert observer._relationship_motive is False


def test_decision_instruction_has_no_ceiling_or_blacklist() -> None:
    from app.config.relationship_initiative import (
        expression_bias_guidance,
        relationship_decision_instruction,
    )

    text = relationship_decision_instruction("natural")
    assert "先判断这是不是她此刻真实会做的事" in text
    assert "不为了证明主动而制造欲望" in text
    assert "不把屏幕内容硬拗成亲密理由" in text
    assert "最多只能轻触" not in text
    assert "不得直接露骨" not in text
    assert expression_bias_guidance("natural") in text


def test_decision_failure_is_silent_without_template() -> None:
    observer = _obs()
    spoken: list = []
    observer.on_speak = lambda payload: spoken.append(payload)
    observer._post_speech_decision = AsyncMock(return_value=None)
    asyncio.run(observer._do_relationship_evaluation())
    assert spoken == []
    assert observer._last_relationship_silent_at > 0
    assert observer._last_relationship_spoken_at == 0.0
    assert observer._last_proactive_at == 0.0


def test_speak_uses_relationship_source_and_independent_cooldown() -> None:
    observer = _obs()
    spoken: list = []
    observer.on_speak = lambda payload: spoken.append(payload)
    observer._relationship_generation = 7
    observer._post_speech_decision = AsyncMock(
        return_value={
            "should_speak": True,
            "reason": "想靠近",
            "comment": "こっち。",
            "translation": "过来。",
            "tone": "温柔",
        }
    )
    asyncio.run(observer._do_relationship_evaluation())
    assert len(spoken) == 1
    assert spoken[0].source == "relationship"
    assert spoken[0].generation == 7
    assert spoken[0].text == "こっち。"
    assert observer._last_relationship_spoken_at > 0
    assert observer._last_proactive_at == 0.0
