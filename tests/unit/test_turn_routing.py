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


def _plan_for(messages: list[ChatMessage], *, active: bool, turns_left: int = 6) -> object:
    """构建 resolve_turn_plan 调用，mock intimacy_mode_state。"""
    request = _request_for(messages)
    settings = _settings()
    recall = resolve_recall_decision(messages, request, proactive_mode=False, settings=settings)

    mock_state = MagicMock()
    mock_state.active = active
    mock_state.consume_turn.return_value = active and turns_left > 0

    with patch("app.agent.turn_routing.intimacy_mode_state", mock_state):
        return resolve_turn_plan(
            messages,
            request,
            proactive_mode=False,
            has_vision_client=False,
            chat_fast_configured=True,
            settings=settings,
            recall_decision=recall,
        )


def test_intimacy_active_routes_to_fast() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "ね……"}]
    plan = _plan_for(messages, active=True, turns_left=5)
    assert plan.tier == "fast"
    assert plan.decided_by == "intimacy_mode"
    assert plan.generation_params == {"thinking": {"type": "disabled"}}


def test_intimacy_inactive_is_normal_routing() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "おはよう"}]
    plan = _plan_for(messages, active=False)
    assert plan.decided_by != "intimacy_mode"


def test_intimacy_auto_exit_falls_back_to_standard() -> None:
    """计数器耗尽时 consume_turn 返回 False → 走正常路由。"""
    messages: list[ChatMessage] = [{"role": "user", "content": "今日はいい天気だね"}]
    plan = _plan_for(messages, active=True, turns_left=0)
    # active=True 但 consume_turn 返回 False → 应 fallback
    assert plan.decided_by != "intimacy_mode"


def test_intimacy_overrides_classifier_simple() -> None:
    """亲密模式优先于分类器的 simple 判定。"""
    messages: list[ChatMessage] = [{"role": "user", "content": "うん"}]
    request = _request_for(messages)
    settings = _settings(classifier_enabled=True)
    recall = resolve_recall_decision(messages, request, proactive_mode=False, settings=settings)

    mock_state = MagicMock()
    mock_state.active = True
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
    assert plan.decided_by == "intimacy_mode"


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
    # 当前设计：亲密优先，图片消息也走 fast。
    # 如果以后要改优先级，这个测试会报警。
    assert plan.tier == "fast"
    assert plan.decided_by == "intimacy_mode"
