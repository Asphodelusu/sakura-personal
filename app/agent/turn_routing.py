"""Turn Orchestrator — Recall Gate 与 Turn Router 纯函数层。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from app.agent.tool_routing import (
    _latest_user_text,
    infer_active_tool_groups_from_messages,
    user_requests_memory_recall,
    user_requests_memory_remember,
)
from app.agent.context_orchestrator import build_context_request
from app.llm.api_client import ChatMessage, messages_contain_image
from app.llm.prompts.types import ContextRequest

RecallDecision = Literal["skip", "recall", "defer", "light"]
TurnTier = Literal["fast", "standard"]
TurnModality = Literal["text", "vision"]

_LONG_INPUT_CHARS = 200

# fast 白名单：单向寒暄或纯确认，几乎不需要接上下文、也不期待对方「展开说」。
_SIMPLE_GREETING_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^(你好|嗨|哈喽|hello|hi|hey|早安|早上好|午安|晚安)[\s!！?？~～。.]*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(ok|okay|好的|好哒|嗯|嗯嗯|收到|明白|知道了)[\s!！?？~～。.]*$",
        re.IGNORECASE,
    ),
)

# 在场探询：中文里常是「有话要说」的前奏；重复出现更容易惹烦，需接话而非模板快答。
_PRESENCE_PROBE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(在吗|在不在|在么|在嘛|还在吗|你还在吗)[\s!！?？~～。.]*$", re.IGNORECASE),
    re.compile(r"^(在吗){2,}[\s!！?？~～。.]*$"),
)

# 空闲/状态探询：期待根据关系与当下状态回应，不是纯寒暄。
_AVAILABILITY_PROBE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^(忙吗|有空吗|方便吗|现在方便吗|有空聊吗|能聊吗|方便聊吗|现在有空吗)[\s!！?？~～。.]*$"
    ),
)

# 社交开场 / 活动探询：隐含继续聊下去的意图。
_SOCIAL_OPENING_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(在干嘛|在干啥|干啥呢|干什么呢|做什么|干嘛呢)[\s!！?？~～。.]*$"),
    re.compile(r"^(聊聊|聊聊天|随便聊聊|聊两句|说说话|聊会天)[\s!！?？~～。.]*$"),
)

# 短句追问（≤ simple_greeting_max_chars）：哪怕很短也像在等真实回应。
_SHORT_QUESTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^.+[吗嘛么][\s?？!！~～。.]*$"),
    re.compile(r"^.+[呢][\s?？!！~～。.]*$"),
)

_JUDGMENT_SEEK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(你觉得|你认为|怎么看|行不行|好不好|可以吗|可不可以|能不能)"),
    re.compile(r"^你觉得呢[\s?？!！~～。.]*$"),
    re.compile(r"^怎么样[\s?？!！~～。.]*$"),
)

_HISTORY_REFERENCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(上次|昨天|前天|之前|早些时候|刚才我们|还记得吗)"),
    re.compile(r"(what did (i|we)|do you remember|last time)", re.IGNORECASE),
)

_TOOL_TASK_KEYWORDS: tuple[str, ...] = (
    "打开",
    "搜索",
    "搜一下",
    "查一下",
    "帮我",
    "点击",
    "截图",
    "浏览器",
    "http://",
    "https://",
    "todo",
    "待办",
    "提醒",
)


@dataclass(frozen=True)
class TurnRoutingSettings:
    enabled: bool = True
    classifier_enabled: bool = False
    backchannel_orchestration_enabled: bool = False
    simple_greeting_max_chars: int = 12
    classifier_timeout_seconds: int = 1


@dataclass(frozen=True)
class BackchannelScheduleHint:
    should_schedule: bool
    phase: str | None
    reason: str


@dataclass(frozen=True)
class TurnPlan:
    tier: TurnTier
    modality: TurnModality
    client_key: Literal["chat", "chat_fast", "vision"]
    generation_params: dict[str, Any] = field(default_factory=dict)
    recall_decision: RecallDecision = "defer"
    decided_by: str = "default"


@dataclass(frozen=True)
class TurnState:
    turn_plan: TurnPlan
    recall_decision: RecallDecision


def resolve_backchannel_schedule(
    messages: list[ChatMessage],
    *,
    proactive_mode: bool = False,
    has_vision_client: bool,
    chat_fast_configured: bool,
    settings: TurnRoutingSettings,
) -> BackchannelScheduleHint:
    """预检是否调度等待期接话;不调用 LLM 分类器,未决场景按 standard 处理。"""
    if not settings.backchannel_orchestration_enabled:
        return BackchannelScheduleHint(
            should_schedule=True,
            phase=None,
            reason="orchestration_disabled",
        )

    if not settings.enabled:
        return BackchannelScheduleHint(
            should_schedule=True,
            phase="long_wait",
            reason="routing_disabled",
        )

    request = build_context_request(
        messages,
        source="chat",
        mode="normal",
        event_type="",
        step_index=0,
        remaining_steps=3,
        available_tools=(),
    )
    recall = resolve_recall_decision(
        messages, request, proactive_mode=proactive_mode, settings=settings
    )
    if recall == "skip" or _is_simple_greeting(messages, settings):
        return BackchannelScheduleHint(
            should_schedule=False,
            phase=None,
            reason="recall_skip" if recall == "skip" else "simple_greeting",
        )

    plan = resolve_turn_plan(
        messages,
        request,
        proactive_mode=proactive_mode,
        has_vision_client=has_vision_client,
        chat_fast_configured=chat_fast_configured,
        settings=settings,
        classifier_result=None,
        recall_decision=recall,
    )
    if plan.tier == "fast":
        return BackchannelScheduleHint(
            should_schedule=False,
            phase=None,
            reason="fast_tier",
        )

    return BackchannelScheduleHint(
        should_schedule=True,
        phase="long_wait",
        reason=plan.decided_by,
    )


def resolve_recall_decision(
    messages: list[ChatMessage],
    request: ContextRequest,
    *,
    proactive_mode: bool,
    settings: TurnRoutingSettings,
) -> RecallDecision:
    if not settings.enabled:
        return "recall"
    if proactive_mode:
        return "recall"
    if user_requests_memory_recall(messages) or _has_history_reference(messages):
        return "recall"
    if _is_simple_greeting(messages, settings):
        return "skip"
    # 普通对话默认轻量召回：连续性上下文 + 1-2 条相关情节记忆
    return "light"


def resolve_turn_plan(
    messages: list[ChatMessage],
    request: ContextRequest,
    *,
    proactive_mode: bool,
    has_vision_client: bool,
    chat_fast_configured: bool,
    settings: TurnRoutingSettings,
    classifier_result: Literal["simple", "deep"] | None = None,
    recall_decision: RecallDecision | None = None,
) -> TurnPlan:
    recall = recall_decision or resolve_recall_decision(
        messages, request, proactive_mode=proactive_mode, settings=settings
    )
    if not settings.enabled:
        return _standard_plan(recall_decision=recall, decided_by="disabled")

    has_image = messages_contain_image(messages)
    if proactive_mode:
        return _standard_plan(recall_decision=recall, decided_by="proactive_mode")
    if has_image:
        client_key: Literal["chat", "chat_fast", "vision"] = (
            "vision" if has_vision_client else "chat"
        )
        return TurnPlan(
            tier="standard",
            modality="vision",
            client_key=client_key,
            generation_params={},
            recall_decision=recall,
            decided_by="vision_input",
        )
    if not chat_fast_configured:
        return _standard_plan(recall_decision=recall, decided_by="no_chat_fast")

    if user_requests_memory_recall(messages) or recall == "recall":
        return _standard_plan(recall_decision=recall, decided_by="memory_recall")
    if user_requests_memory_remember(messages):
        return _standard_plan(recall_decision=recall, decided_by="memory_remember")
    if _is_long_input(messages):
        return _standard_plan(recall_decision=recall, decided_by="long_input")
    if _has_consecutive_user_messages(messages):
        return _standard_plan(recall_decision=recall, decided_by="consecutive_user")
    if _has_tool_task_intent(messages):
        return _standard_plan(recall_decision=recall, decided_by="tool_task")

    engaged_by = _engaged_reply_decision(messages, settings)
    if engaged_by is not None:
        return _standard_plan(recall_decision=recall, decided_by=engaged_by)

    if _is_simple_greeting(messages, settings):
        return _fast_plan(recall_decision=recall, decided_by="simple_greeting")

    if settings.classifier_enabled and classifier_result is not None:
        if classifier_result == "simple":
            return _fast_plan(recall_decision=recall, decided_by="classifier:simple")
        return _standard_plan(recall_decision=recall, decided_by="classifier:deep")

    return _standard_plan(recall_decision=recall, decided_by="default")


def _standard_plan(
    *,
    recall_decision: RecallDecision,
    decided_by: str,
) -> TurnPlan:
    return TurnPlan(
        tier="standard",
        modality="text",
        client_key="chat",
        generation_params={},
        recall_decision=recall_decision,
        decided_by=decided_by,
    )


def _fast_plan(
    *,
    recall_decision: RecallDecision,
    decided_by: str,
) -> TurnPlan:
    return TurnPlan(
        tier="fast",
        modality="text",
        client_key="chat_fast",
        generation_params={"thinking": {"type": "disabled"}},
        recall_decision=recall_decision,
        decided_by=decided_by,
    )


def _engaged_reply_decision(
    messages: list[ChatMessage],
    settings: TurnRoutingSettings,
) -> str | None:
    """需接话、看语境的短输入 → 不走 fast。"""
    text = (_latest_user_text(messages) or "").strip()
    if not text:
        return None

    if any(pattern.match(text) for pattern in _PRESENCE_PROBE_PATTERNS):
        if _presence_probe_count(messages) >= 2:
            return "repeated_presence_probe"
        return "presence_probe"
    if any(pattern.match(text) for pattern in _AVAILABILITY_PROBE_PATTERNS):
        return "availability_probe"
    if any(pattern.match(text) for pattern in _SOCIAL_OPENING_PATTERNS):
        return "social_opening"

    if len(text) <= settings.simple_greeting_max_chars:
        if any(pattern.match(text) for pattern in _SHORT_QUESTION_PATTERNS):
            return "short_question"
        if any(pattern.match(text) for pattern in _JUDGMENT_SEEK_PATTERNS):
            return "judgment_seek"

    return None


def _presence_probe_count(messages: list[ChatMessage]) -> int:
    count = 0
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        text = content.strip() if isinstance(content, str) else ""
        if text and any(pattern.match(text) for pattern in _PRESENCE_PROBE_PATTERNS):
            count += 1
    return count


def _is_simple_greeting(messages: list[ChatMessage], settings: TurnRoutingSettings) -> bool:
    if _engaged_reply_decision(messages, settings) is not None:
        return False
    text = (_latest_user_text(messages) or "").strip()
    if not text or len(text) > settings.simple_greeting_max_chars:
        return False
    if user_requests_memory_recall(messages) or user_requests_memory_remember(messages):
        return False
    if _has_tool_task_intent(messages):
        return False
    return any(pattern.match(text) for pattern in _SIMPLE_GREETING_PATTERNS)


def should_invoke_turn_classifier(
    messages: list[ChatMessage],
    *,
    proactive_mode: bool,
    chat_fast_configured: bool,
    settings: TurnRoutingSettings,
    recall_decision: RecallDecision,
) -> bool:
    if not settings.enabled or not settings.classifier_enabled:
        return False
    if proactive_mode or messages_contain_image(messages) or not chat_fast_configured:
        return False
    if user_requests_memory_recall(messages) or recall_decision == "recall":
        return False
    if user_requests_memory_remember(messages):
        return False
    if _is_long_input(messages) or _has_consecutive_user_messages(messages) or _has_tool_task_intent(messages):
        return False
    if _is_simple_greeting(messages, settings):
        return False
    if _engaged_reply_decision(messages, settings) is not None:
        return False
    return True


def _has_history_reference(messages: list[ChatMessage]) -> bool:
    text = (_latest_user_text(messages) or "").strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _HISTORY_REFERENCE_PATTERNS)


def _is_long_input(messages: list[ChatMessage]) -> bool:
    text = (_latest_user_text(messages) or "").strip()
    return len(text) > _LONG_INPUT_CHARS


def _has_consecutive_user_messages(messages: list[ChatMessage]) -> bool:
    trailing_users = 0
    for message in reversed(messages):
        if message.get("role") != "user":
            break
        trailing_users += 1
    return trailing_users >= 2


def _has_tool_task_intent(messages: list[ChatMessage]) -> bool:
    groups = infer_active_tool_groups_from_messages(messages)
    if groups - {"core"}:
        return True
    text = (_latest_user_text(messages) or "").lower()
    return any(keyword.lower() in text for keyword in _TOOL_TASK_KEYWORDS)
