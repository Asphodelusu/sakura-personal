"""Relationship initiative: the single arbiter for self-initiated turns and its decision call.

Gates and backoff follow the Qt-era ProactiveObserver. A decision may choose silence;
silence is an ordinary outcome, not a failure.
"""

from __future__ import annotations

import json
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from app.config.relationship_initiative import (
    RELATIONSHIP_SILENT_BACKOFF_SECONDS,
    RelationshipInitiativeSettings,
    relationship_decision_instruction,
)
from app.llm.chat_reply import ChatReply, ChatSegment

DECISION_TEMPERATURE = 0.7
DECISION_MAX_TOKENS = 512


def get_idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input on this desktop (Windows only)."""
    if sys.platform != "win32":
        return 0.0
    import ctypes
    from ctypes import wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    elapsed_ms = (ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
    return elapsed_ms / 1000.0


class InitiativeArbiter:
    """Owns the timing state that every self-initiated turn has to respect."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        idle_seconds: Callable[[], float] = get_idle_seconds,
    ) -> None:
        self._clock = clock
        self._idle_seconds = idle_seconds
        self.settings = RelationshipInitiativeSettings(proactive_enabled=False).normalized()
        # Starting the app counts as contact; nothing is said before the silence window.
        self._last_user_at = clock()
        self._last_spoken_at = 0.0
        self._last_silent_at = 0.0
        self._silence_streak = 0
        self._generation = 0

    def configure(self, settings: RelationshipInitiativeSettings) -> None:
        self.settings = settings.normalized()

    def note_user_spoke(self) -> None:
        self._last_user_at = self._clock()
        self._generation += 1
        self._reset_backoff()

    def seconds_since_user(self) -> int:
        return max(0, int(self._clock() - self._last_user_at))

    def _silent_cooldown_seconds(self) -> float:
        if self._silence_streak <= 0:
            return RELATIONSHIP_SILENT_BACKOFF_SECONDS[0]
        index = min(self._silence_streak - 1, len(RELATIONSHIP_SILENT_BACKOFF_SECONDS) - 1)
        return RELATIONSHIP_SILENT_BACKOFF_SECONDS[index]

    def _reset_backoff(self) -> None:
        self._silence_streak = 0
        self._last_silent_at = 0.0

    def gate_reason(self, *, busy: bool = False, continuation: bool = False) -> str:
        if not self.settings.proactive_enabled:
            return "disabled"
        if continuation:
            return "continuation"
        if busy:
            return "busy"
        now = self._clock()
        if now - self._last_user_at < float(self.settings.proactive_min_silence_seconds):
            return "silence"
        if self._last_spoken_at and now - self._last_spoken_at < float(self.settings.proactive_cooldown_seconds):
            return "cooldown"
        if self._last_silent_at and now - self._last_silent_at < self._silent_cooldown_seconds():
            return "cooldown"
        try:
            idle = float(self._idle_seconds())
        except Exception:  # noqa: BLE001 - an unreadable idle clock never blocks the gate
            idle = 0.0
        if idle >= float(self.settings.desktop_idle_seconds):
            return "desktop_idle"
        return "eligible"

    def begin_attempt(self) -> int:
        return self._generation

    def is_current(self, attempt: int) -> bool:
        return attempt == self._generation

    def mark_silent(self) -> None:
        self._last_silent_at = self._clock()
        self._silence_streak = min(self._silence_streak + 1, 4)

    def mark_spoken(self) -> None:
        self._reset_backoff()
        self._last_spoken_at = self._clock()


def build_relationship_decision_messages(
    *,
    system_prompt: str,
    relationship_guide: str,
    expression_bias: str,
    now_iso: str,
    since_user_seconds: int,
    recent_dialogue: str,
    relationship_facts: str,
    drive_summary: str,
) -> tuple[str, list[dict[str, str]]]:
    from app.llm.prompts.personal_persona import (
        extract_character_identity_anchor,
        select_character_behavior_core,
        select_relationship_guide_core_sections,
    )

    guide = "\n\n".join(body for _id, body in select_relationship_guide_core_sections(relationship_guide))
    persona = "\n\n".join(
        part.strip()
        for part in (
            extract_character_identity_anchor(system_prompt),
            select_character_behavior_core(system_prompt),
            guide,
        )
        if part and part.strip()
    )
    instruction = relationship_decision_instruction(expression_bias)
    system = f"{persona}\n\n{instruction}" if persona else instruction
    parts = [f"[当前时间]\n{now_iso}", f"[距上次互动]\n{int(since_user_seconds)}s"]
    if recent_dialogue.strip():
        parts.append(recent_dialogue.strip())
    if relationship_facts.strip():
        parts.append(relationship_facts.strip())
    if drive_summary.strip():
        parts.append(f"[当前亲近倾向]\n{drive_summary.strip()}")
    return system, [{"role": "user", "content": "\n\n".join(parts)}]


def parse_relationship_decision(raw: object) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = [fenced.group(1)] if fenced else []
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def decision_to_reply(decision: Mapping[str, Any] | None, *, allowed_tones: Sequence[str]) -> ChatReply | None:
    if not isinstance(decision, Mapping) or decision.get("should_speak") is not True:
        return None
    comment = str(decision.get("comment") or "").strip()
    if not comment:
        return None
    tone = str(decision.get("tone") or "").strip()
    if allowed_tones and tone not in allowed_tones:
        tone = allowed_tones[0]
    return ChatReply([ChatSegment(comment, tone or "中性", str(decision.get("translation") or "").strip())])
