from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

EXPRESSION_BIASES = ("restrained", "natural", "expressive")
DEFAULT_EXPRESSION_BIAS = "natural"
DEFAULT_PROACTIVE_COOLDOWN_SECONDS = 3600
DEFAULT_PROACTIVE_MIN_SILENCE_SECONDS = 300
DEFAULT_DESKTOP_IDLE_SECONDS = 900
RELATIONSHIP_SILENT_COOLDOWN_SECONDS = 300.0
RELATIONSHIP_SILENT_BACKOFF_SECONDS = (300.0, 600.0, 1200.0, 1800.0)
RELATIONSHIP_GUIDE_TOKEN_BUDGET = 1600
COOLDOWN_MIN_SECONDS = 60
COOLDOWN_MAX_SECONDS = 86400
SILENCE_MIN_SECONDS = 30
SILENCE_MAX_SECONDS = 3600
DESKTOP_IDLE_MIN_SECONDS = 60
DESKTOP_IDLE_MAX_SECONDS = 86400
RELATIONSHIP_GATE_REASONS = (
    "disabled",
    "busy",
    "silence",
    "cooldown",
    "continuation",
    "desktop_idle",
    "eligible",
)

_BIAS_GUIDANCE = {
    "restrained": (
        "表达倾向：restrained。已经产生的靠近或亲密想法，多数时候先放在心里或只露出一点；"
        "只有当下关系、气氛和对方反应都明确支持时才说出口。不增加内容限制，也不缩小可做的事。"
    ),
    "natural": (
        "表达倾向：natural。平时克制；认定关系且当下确实起意后可以直接。"
        "允许从轻触、邀请陪伴到直接表达想要，由她自己判断；不要把已经产生的欲望系统降级成只能拥抱或索吻。"
    ),
    "expressive": (
        "表达倾向：expressive。已经产生的想法更容易说出来或做出来，"
        "但仍必须是她此刻真实会做的事，不为了证明主动而制造欲望。不增加内容限制。"
    ),
}

_DECISION_INSTRUCTION = """现在不是看屏幕找话题，而是判断你（夜乃桜）要不要因关系和自身状态主动开口。
对方是「他」。可以选择沉默。

先判断这是不是她此刻真实会做的事。
不为了证明主动而制造欲望。
不把屏幕内容硬拗成亲密理由。
对方真实退开、拒绝或不适时，尊重当下反应。
不要复述上一句主动发言。
一次主动不是全天黏人；一次拒绝不是永久禁欲。

should_speak=true：comment 用她会说出口的日语 1〜3 句，translation 简体中文，tone 用可用语气。
should_speak=false：comment/translation/tone 可空。
reason：简体中文一句，用于日志，不要写入对白。

{"should_speak":true|false,"reason":"…","comment":"…","translation":"…","tone":"温柔"}"""


def expression_bias_guidance(bias: str) -> str:
    key = str(bias or "").strip().lower()
    return _BIAS_GUIDANCE.get(key, _BIAS_GUIDANCE[DEFAULT_EXPRESSION_BIAS])


def relationship_decision_instruction(bias: str) -> str:
    return f"{_DECISION_INSTRUCTION}\n\n{expression_bias_guidance(bias)}"


@dataclass(frozen=True)
class RelationshipInitiativeSettings:
    in_turn_enabled: bool = True
    proactive_enabled: bool = True
    expression_bias: str = DEFAULT_EXPRESSION_BIAS
    proactive_cooldown_seconds: int = DEFAULT_PROACTIVE_COOLDOWN_SECONDS
    proactive_min_silence_seconds: int = DEFAULT_PROACTIVE_MIN_SILENCE_SECONDS
    desktop_idle_seconds: int = DEFAULT_DESKTOP_IDLE_SECONDS

    def normalized(self) -> "RelationshipInitiativeSettings":
        bias = str(self.expression_bias or "").strip().lower()
        if bias not in EXPRESSION_BIASES:
            bias = DEFAULT_EXPRESSION_BIAS
        return RelationshipInitiativeSettings(
            in_turn_enabled=bool(self.in_turn_enabled),
            proactive_enabled=bool(self.proactive_enabled),
            expression_bias=bias,
            proactive_cooldown_seconds=_clamp_int(
                self.proactive_cooldown_seconds,
                DEFAULT_PROACTIVE_COOLDOWN_SECONDS,
                COOLDOWN_MIN_SECONDS,
                COOLDOWN_MAX_SECONDS,
            ),
            proactive_min_silence_seconds=_clamp_int(
                self.proactive_min_silence_seconds,
                DEFAULT_PROACTIVE_MIN_SILENCE_SECONDS,
                SILENCE_MIN_SECONDS,
                SILENCE_MAX_SECONDS,
            ),
            desktop_idle_seconds=_clamp_int(
                self.desktop_idle_seconds,
                DEFAULT_DESKTOP_IDLE_SECONDS,
                DESKTOP_IDLE_MIN_SECONDS,
                DESKTOP_IDLE_MAX_SECONDS,
            ),
        )


def settings_from_mapping(raw: Mapping[str, Any] | None) -> RelationshipInitiativeSettings:
    source = dict(raw) if isinstance(raw, Mapping) else {}
    return RelationshipInitiativeSettings(
        in_turn_enabled=_as_bool(source.get("in_turn_enabled"), True),
        proactive_enabled=_as_bool(source.get("proactive_enabled"), True),
        expression_bias=str(source.get("expression_bias", DEFAULT_EXPRESSION_BIAS) or DEFAULT_EXPRESSION_BIAS),
        proactive_cooldown_seconds=_as_int(
            source.get("proactive_cooldown_seconds"),
            DEFAULT_PROACTIVE_COOLDOWN_SECONDS,
        ),
        proactive_min_silence_seconds=_as_int(
            source.get("proactive_min_silence_seconds"),
            DEFAULT_PROACTIVE_MIN_SILENCE_SECONDS,
        ),
        desktop_idle_seconds=_as_int(
            source.get("desktop_idle_seconds"),
            DEFAULT_DESKTOP_IDLE_SECONDS,
        ),
    ).normalized()


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    if parsed < 0:
        return default
    return parsed


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    parsed = _as_int(value, default)
    return max(minimum, min(maximum, parsed))
