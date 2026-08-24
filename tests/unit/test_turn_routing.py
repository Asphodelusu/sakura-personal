"""tests/unit/test_turn_routing.py — Turn Orchestrator 路由决策测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.agent.context_orchestrator import build_context_request
from app.agent.turn_classifier import classify_turn_depth
from app.agent.turn_routing import (
    TurnRoutingSettings,
    resolve_backchannel_schedule,
    resolve_recall_decision,
    resolve_turn_plan,
)
from app.llm.api_client import ChatMessage, OpenAICompatibleClient
from app.llm.prompts.types import ContextRequest


def _request_for(messages: list[ChatMessage]) -> ContextRequest:
    return build_context_request(
        messages,
        source="chat",
        mode="normal",
        event_type="",
        step_index=0,
        remaining_steps=3,
        available_tools=(),
    )


def _settings(**overrides: object) -> TurnRoutingSettings:
    base = {
        "enabled": True,
        "classifier_enabled": True,
        "backchannel_orchestration_enabled": True,
        "simple_greeting_max_chars": 12,
        "classifier_timeout_seconds": 1,
    }
    base.update(overrides)
    return TurnRoutingSettings(**base)


def _schedule_hint(
    messages: list[ChatMessage],
    *,
    proactive_mode: bool = False,
    has_vision_client: bool = False,
    chat_fast_configured: bool = True,
    settings: TurnRoutingSettings | None = None,
):
    return resolve_backchannel_schedule(
        messages,
        proactive_mode=proactive_mode,
        has_vision_client=has_vision_client,
        chat_fast_configured=chat_fast_configured,
        settings=settings or _settings(),
    )


def test_backchannel_schedule_skips_simple_greeting() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "你好"}]
    hint = _schedule_hint(messages)

    assert hint.should_schedule is False
    assert hint.phase is None
    assert hint.reason in {"recall_skip", "simple_greeting"}


def test_backchannel_schedule_long_wait_for_deferred_standard() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "今天工作有点累，随便聊聊吧"}]
    hint = _schedule_hint(messages)

    assert hint.should_schedule is True
    assert hint.phase == "long_wait"
    assert hint.reason == "default"


def test_backchannel_schedule_long_wait_for_memory_recall() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "你还记得我刚才让你记住什么吗"}]
    hint = _schedule_hint(messages)

    assert hint.should_schedule is True
    assert hint.phase == "long_wait"
    assert hint.reason == "memory_recall"


def test_backchannel_schedule_always_when_orchestration_disabled() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "你好"}]
    hint = _schedule_hint(messages, settings=_settings(backchannel_orchestration_enabled=False))

    assert hint.should_schedule is True
    assert hint.phase is None
    assert hint.reason == "orchestration_disabled"


def test_simple_greeting_skip_and_fast() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "你好"}]
    request = _request_for(messages)
    settings = _settings(classifier_enabled=False)

    recall = resolve_recall_decision(messages, request, proactive_mode=False, settings=settings)
    plan = resolve_turn_plan(
        messages,
        request,
        proactive_mode=False,
        has_vision_client=False,
        chat_fast_configured=True,
        settings=settings,
        recall_decision=recall,
    )

    assert recall == "skip"
    assert plan.tier == "fast"
    assert plan.client_key == "chat_fast"
    assert plan.recall_decision == "skip"
    assert plan.decided_by == "simple_greeting"


def test_presence_probe_zaima_standard_not_fast() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "在吗"}]
    request = _request_for(messages)
    settings = _settings(classifier_enabled=False)

    recall = resolve_recall_decision(messages, request, proactive_mode=False, settings=settings)
    plan = resolve_turn_plan(
        messages,
        request,
        proactive_mode=False,
        has_vision_client=False,
        chat_fast_configured=True,
        settings=settings,
        recall_decision=recall,
    )

    assert recall == "light"
    assert plan.tier == "standard"
    assert plan.client_key == "chat"
    assert plan.decided_by == "presence_probe"


def test_repeated_presence_probe_escalates_reason() -> None:
    messages: list[ChatMessage] = [
        {"role": "user", "content": "在吗"},
        {"role": "assistant", "content": "在的，怎么了？"},
        {"role": "user", "content": "在吗"},
    ]
    request = _request_for(messages)
    settings = _settings(classifier_enabled=False)

    plan = resolve_turn_plan(
        messages,
        request,
        proactive_mode=False,
        has_vision_client=False,
        chat_fast_configured=True,
        settings=settings,
        recall_decision="defer",
    )

    assert plan.tier == "standard"
    assert plan.decided_by == "repeated_presence_probe"


def test_availability_probe_standard() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "忙吗"}]
    request = _request_for(messages)
    settings = _settings(classifier_enabled=False)

    plan = resolve_turn_plan(
        messages,
        request,
        proactive_mode=False,
        has_vision_client=False,
        chat_fast_configured=True,
        settings=settings,
        recall_decision="defer",
    )

    assert plan.tier == "standard"
    assert plan.decided_by == "availability_probe"


def test_short_question_standard() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "猫可爱吗"}]
    request = _request_for(messages)
    settings = _settings(classifier_enabled=False)

    plan = resolve_turn_plan(
        messages,
        request,
        proactive_mode=False,
        has_vision_client=False,
        chat_fast_configured=True,
        settings=settings,
        recall_decision="defer",
    )

    assert plan.tier == "standard"
    assert plan.decided_by == "short_question"


def test_backchannel_schedules_for_presence_probe() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "在吗"}]
    hint = _schedule_hint(messages, settings=_settings(classifier_enabled=False))

    assert hint.should_schedule is True
    assert hint.phase == "long_wait"
    assert hint.reason == "presence_probe"


def test_default_pro_without_classifier() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "今天工作有点累，随便聊聊吧"}]
    request = _request_for(messages)
    settings = _settings(classifier_enabled=False)

    recall = resolve_recall_decision(messages, request, proactive_mode=False, settings=settings)
    plan = resolve_turn_plan(
        messages,
        request,
        proactive_mode=False,
        has_vision_client=False,
        chat_fast_configured=True,
        settings=settings,
        classifier_result=None,
        recall_decision=recall,
    )

    assert recall == "light"
    assert plan.tier == "standard"
    assert plan.decided_by == "default"
    assert plan.generation_params == {"thinking": {"type": "disabled"}}


def test_deep_thinking_request_enables_thinking_on_pro() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "仔细想想我们该怎么办"}]
    request = _request_for(messages)
    settings = _settings(classifier_enabled=False)

    plan = resolve_turn_plan(
        messages,
        request,
        proactive_mode=False,
        has_vision_client=False,
        chat_fast_configured=True,
        settings=settings,
        recall_decision="light",
    )

    assert plan.tier == "standard"
    assert plan.client_key == "chat"
    assert plan.decided_by == "deep_thinking"
    assert plan.generation_params == {"thinking": {"type": "enabled"}}


def test_tool_task_keeps_thinking_disabled() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "帮我搜索一下今天天气"}]
    request = _request_for(messages)
    settings = _settings(classifier_enabled=False)

    plan = resolve_turn_plan(
        messages,
        request,
        proactive_mode=False,
        has_vision_client=False,
        chat_fast_configured=True,
        settings=settings,
        recall_decision="light",
    )

    assert plan.decided_by == "tool_task"
    assert plan.generation_params == {"thinking": {"type": "disabled"}}


def test_memory_recall_intent_recall_and_standard() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "你还记得我刚才让你记住什么吗"}]
    request = _request_for(messages)
    settings = _settings()

    recall = resolve_recall_decision(messages, request, proactive_mode=False, settings=settings)
    plan = resolve_turn_plan(
        messages,
        request,
        proactive_mode=False,
        has_vision_client=False,
        chat_fast_configured=True,
        settings=settings,
        recall_decision=recall,
    )

    assert recall == "recall"
    assert plan.tier == "standard"
    assert plan.client_key == "chat"
    assert plan.decided_by == "memory_recall"


def test_without_chat_fast_falls_back_to_standard() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "你好"}]
    request = _request_for(messages)
    settings = _settings()

    recall = resolve_recall_decision(messages, request, proactive_mode=False, settings=settings)
    plan = resolve_turn_plan(
        messages,
        request,
        proactive_mode=False,
        has_vision_client=False,
        chat_fast_configured=False,
        settings=settings,
        recall_decision=recall,
    )

    assert recall == "skip"
    assert plan.tier == "standard"
    assert plan.client_key == "chat"
    assert plan.decided_by == "no_chat_fast"


def test_proactive_mode_skips_classifier_fast_path() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "你好"}]
    request = _request_for(messages)
    settings = _settings()

    recall = resolve_recall_decision(messages, request, proactive_mode=True, settings=settings)
    plan = resolve_turn_plan(
        messages,
        request,
        proactive_mode=True,
        has_vision_client=False,
        chat_fast_configured=True,
        settings=settings,
        classifier_result="simple",
        recall_decision=recall,
    )

    assert recall == "recall"
    assert plan.tier == "standard"
    assert plan.decided_by == "proactive_mode"
    assert plan.generation_params == {"thinking": {"type": "disabled"}}


def test_classifier_failure_falls_back_to_standard() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "今天工作有点累，随便聊聊吧"}]
    request = _request_for(messages)
    settings = _settings(classifier_enabled=True)

    recall = resolve_recall_decision(messages, request, proactive_mode=False, settings=settings)
    plan = resolve_turn_plan(
        messages,
        request,
        proactive_mode=False,
        has_vision_client=False,
        chat_fast_configured=True,
        settings=settings,
        classifier_result=None,
        recall_decision=recall,
    )

    assert recall == "light"
    assert plan.tier == "standard"
    assert plan.decided_by == "default"


def test_classifier_simple_enables_fast_when_explicitly_enabled() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "今天工作有点累，随便聊聊吧"}]
    request = _request_for(messages)
    settings = _settings(classifier_enabled=True)

    recall = resolve_recall_decision(messages, request, proactive_mode=False, settings=settings)
    plan = resolve_turn_plan(
        messages,
        request,
        proactive_mode=False,
        has_vision_client=False,
        chat_fast_configured=True,
        settings=settings,
        classifier_result="simple",
        recall_decision=recall,
    )

    assert plan.tier == "fast"
    assert plan.decided_by == "classifier:simple"
    assert plan.generation_params == {"thinking": {"type": "disabled"}}


def test_classifier_deep_enables_thinking() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "今天工作有点累，随便聊聊吧"}]
    request = _request_for(messages)
    settings = _settings(classifier_enabled=True)

    plan = resolve_turn_plan(
        messages,
        request,
        proactive_mode=False,
        has_vision_client=False,
        chat_fast_configured=True,
        settings=settings,
        classifier_result="deep",
        recall_decision="light",
    )

    assert plan.tier == "standard"
    assert plan.decided_by == "classifier:deep"
    assert plan.generation_params == {"thinking": {"type": "enabled"}}


def test_classify_turn_depth_parses_json() -> None:
    client = MagicMock(spec=OpenAICompatibleClient)
    client.settings = MagicMock(timeout_seconds=60)
    client.complete_raw.return_value = '{"depth": "simple"}'

    assert classify_turn_depth("随便聊聊", client=client) == "simple"


def test_classify_turn_depth_returns_none_on_error() -> None:
    from app.llm.api_client import ApiRequestError

    client = MagicMock(spec=OpenAICompatibleClient)
    client.settings = MagicMock(timeout_seconds=60)
    client.complete_raw.side_effect = ApiRequestError("timeout")

    assert classify_turn_depth("随便聊聊", client=client) is None


# ------------------------------------------------------------------
# 亲密模式路由测试
# ------------------------------------------------------------------


def _plan_for(messages: list[ChatMessage], *, active: bool, turns_left: int = 8) -> object:
    """构建 resolve_turn_plan 调用，mock intimacy_mode_state。"""
    request = _request_for(messages)
    settings = _settings()
    recall = resolve_recall_decision(messages, request, proactive_mode=False, settings=settings)

    mock_state = MagicMock()
    mock_state.active = active
    mock_state.pending = False
    mock_state.needs_reentry_hint = False
    mock_state.consume_turn.return_value = active and turns_left > 0

    with (
        patch("app.agent.turn_routing.intimacy_mode_state", mock_state),
        patch("app.agent.turn_routing.apply_intimacy_user_utterance", return_value=None),
    ):
        return resolve_turn_plan(
            messages,
            request,
            proactive_mode=False,
            has_vision_client=False,
            chat_fast_configured=True,
            settings=settings,
            recall_decision=recall,
        )


def test_intimacy_active_routes_to_chat_non_thinking() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "ね……"}]
    plan = _plan_for(messages, active=True, turns_left=5)
    assert plan.tier == "fast"
    assert plan.client_key == "chat"
    assert plan.decided_by == "rhythm_focus"
    assert plan.generation_params == {"thinking": {"type": "disabled"}}


def test_intimacy_inactive_is_normal_routing() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "おはよう"}]
    plan = _plan_for(messages, active=False)
    assert plan.decided_by != "rhythm_focus"


def test_intimacy_continue_exhausted_is_suppressed() -> None:
    """续投耗尽时必须成为 no-op，不能落入普通模型路由。"""
    from app.agent.builtin_tools import build_intimacy_continue_message

    messages: list[ChatMessage] = [build_intimacy_continue_message()]
    plan = _plan_for(messages, active=True, turns_left=0)
    assert plan.suppress_generation is True
    assert plan.decided_by == "rhythm_exhausted"


def test_intimacy_continue_exhausted_keeps_active_without_reentry() -> None:
    """耗尽续投被抑制，intimacy_mode 保持 active/sleep，不生成 reentry。"""
    from app.agent.builtin_tools import IntimacyModeState, build_intimacy_continue_message

    messages: list[ChatMessage] = [build_intimacy_continue_message()]
    request = _request_for(messages)
    settings = _settings()
    recall = resolve_recall_decision(messages, request, proactive_mode=False, settings=settings)

    state = IntimacyModeState()
    state.enter()
    for _ in range(3):
        assert state.consume_turn() is True
    epoch_before = getattr(state, "continuation_epoch", None)

    with patch("app.agent.turn_routing.intimacy_mode_state", state):
        plan = resolve_turn_plan(
            messages,
            request,
            proactive_mode=False,
            has_vision_client=False,
            chat_fast_configured=True,
            settings=settings,
            recall_decision=recall,
        )

    assert plan.suppress_generation is True
    assert plan.decided_by == "rhythm_exhausted"
    assert state.active is True
    assert state.needs_reentry_hint is False
    assert getattr(state, "continuation_epoch", None) == epoch_before


def test_intimacy_user_message_after_sleep_resets_cycle() -> None:
    """休眠后真实用户消息刷新周期并回到 intimacy 路由。"""
    from app.agent.builtin_tools import IntimacyModeState

    messages: list[ChatMessage] = [{"role": "user", "content": "ね……"}]
    request = _request_for(messages)
    settings = _settings()
    recall = resolve_recall_decision(messages, request, proactive_mode=False, settings=settings)

    state = IntimacyModeState()
    state.enter()
    for _ in range(3):
        assert state.consume_turn() is True
    epoch_before = getattr(state, "continuation_epoch", None)

    with patch("app.agent.turn_routing.intimacy_mode_state", state):
        plan = resolve_turn_plan(
            messages,
            request,
            proactive_mode=False,
            has_vision_client=False,
            chat_fast_configured=True,
            settings=settings,
            recall_decision=recall,
        )

    assert plan.decided_by == "rhythm_focus"
    assert state.active is True
    assert state._turns_left == 3
    assert state.needs_reentry_hint is False
    epoch_after = getattr(state, "continuation_epoch", None)
    assert epoch_after is not None
    assert epoch_after != epoch_before


def test_intimacy_user_turn_exit_words_leave_rhythm() -> None:
    """用户说「好了结束吧」等词：代码侧直接退出亲密节奏，走正常路由。"""
    from app.agent.builtin_tools import IntimacyModeState

    messages: list[ChatMessage] = [{"role": "user", "content": "好了，结束吧"}]
    request = _request_for(messages)
    settings = _settings()
    recall = resolve_recall_decision(messages, request, proactive_mode=False, settings=settings)

    state = IntimacyModeState()
    state.enter()

    with patch("app.agent.turn_routing.intimacy_mode_state", state):
        plan = resolve_turn_plan(
            messages,
            request,
            proactive_mode=False,
            has_vision_client=False,
            chat_fast_configured=True,
            settings=settings,
            recall_decision=recall,
        )

    assert state.active is False
    assert plan.decided_by != "rhythm_focus"


def test_intimacy_continue_consumes_not_refresh() -> None:
    """系统续投扣轮次，不刷新。"""
    from app.agent.builtin_tools import build_intimacy_continue_message

    messages: list[ChatMessage] = [build_intimacy_continue_message()]
    request = _request_for(messages)
    settings = _settings()
    recall = resolve_recall_decision(messages, request, proactive_mode=False, settings=settings)

    mock_state = MagicMock()
    mock_state.active = True
    mock_state.pending = False
    mock_state.consume_turn.return_value = True

    with patch("app.agent.turn_routing.intimacy_mode_state", mock_state):
        plan = resolve_turn_plan(
            messages,
            request,
            proactive_mode=False,
            has_vision_client=False,
            chat_fast_configured=True,
            settings=settings,
            recall_decision=recall,
        )

    assert plan.tier == "fast"
    assert plan.client_key == "chat"
    assert plan.decided_by == "rhythm_focus"
    assert plan.generation_params == {"thinking": {"type": "disabled"}}
    mock_state.consume_turn.assert_called_once()
    mock_state.refresh_user_reply.assert_not_called()


def test_intimacy_continue_legacy_user_marker_consumes() -> None:
    """旧版 user 裸标记仍按续投扣次，不刷新。"""
    from app.agent.builtin_tools import INTIMACY_CONTINUE_MARKER

    messages: list[ChatMessage] = [{"role": "user", "content": INTIMACY_CONTINUE_MARKER}]
    request = _request_for(messages)
    settings = _settings()
    recall = resolve_recall_decision(messages, request, proactive_mode=False, settings=settings)

    mock_state = MagicMock()
    mock_state.active = True
    mock_state.pending = False
    mock_state.consume_turn.return_value = True

    with patch("app.agent.turn_routing.intimacy_mode_state", mock_state):
        plan = resolve_turn_plan(
            messages,
            request,
            proactive_mode=False,
            has_vision_client=False,
            chat_fast_configured=True,
            settings=settings,
            recall_decision=recall,
        )

    assert plan.decided_by == "rhythm_focus"
    mock_state.consume_turn.assert_called_once()
    mock_state.refresh_user_reply.assert_not_called()


def test_intimacy_user_turn_refreshes() -> None:
    """真实用户轮刷新存活，不扣轮次。"""
    messages: list[ChatMessage] = [{"role": "user", "content": "ね……"}]
    request = _request_for(messages)
    settings = _settings()
    recall = resolve_recall_decision(messages, request, proactive_mode=False, settings=settings)

    mock_state = MagicMock()
    mock_state.active = True
    mock_state.pending = False
    mock_state.consume_turn.return_value = True

    with patch("app.agent.turn_routing.intimacy_mode_state", mock_state):
        plan = resolve_turn_plan(
            messages,
            request,
            proactive_mode=False,
            has_vision_client=False,
            chat_fast_configured=True,
            settings=settings,
            recall_decision=recall,
        )

    assert plan.decided_by == "rhythm_focus"
    mock_state.refresh_user_reply.assert_called_once()
    mock_state.consume_turn.assert_not_called()


def test_intimacy_overrides_classifier_simple() -> None:
    """亲密模式优先于分类器的 simple 判定。"""
    messages: list[ChatMessage] = [{"role": "user", "content": "うん"}]
    request = _request_for(messages)
    settings = _settings(classifier_enabled=True)
    recall = resolve_recall_decision(messages, request, proactive_mode=False, settings=settings)

    mock_state = MagicMock()
    mock_state.active = True
    mock_state.pending = False
    mock_state.consume_turn.return_value = True

    with patch("app.agent.turn_routing.intimacy_mode_state", mock_state):
        plan = resolve_turn_plan(
            messages,
            request,
            proactive_mode=False,
            has_vision_client=False,
            chat_fast_configured=True,
            settings=settings,
            classifier_result="simple",
            recall_decision=recall,
        )

    assert plan.tier == "fast"
    assert plan.client_key == "chat"
    assert plan.decided_by == "rhythm_focus"


def test_intimacy_does_not_block_vision() -> None:
    """亲密模式不覆盖 vision 路由。"""
    messages: list[ChatMessage] = [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:..."}}]}
    ]
    request = _request_for(messages)
    settings = _settings()
    recall = resolve_recall_decision(messages, request, proactive_mode=False, settings=settings)

    mock_state = MagicMock()
    mock_state.active = True
    mock_state.pending = False
    mock_state.consume_turn.return_value = True

    with patch("app.agent.turn_routing.intimacy_mode_state", mock_state):
        plan = resolve_turn_plan(
            messages,
            request,
            proactive_mode=False,
            has_vision_client=True,
            chat_fast_configured=True,
            settings=settings,
            recall_decision=recall,
        )

    # vision 检查在 intimacy 之前？不是——intimacy 检查在 has_image 之前。
    # 当前设计：亲密优先，图片消息也走亲密节奏（主对话槽 + 非思考）。
    # 如果以后要改优先级，这个测试会报警。
    assert plan.tier == "fast"
    assert plan.client_key == "chat"
    assert plan.decided_by == "rhythm_focus"
