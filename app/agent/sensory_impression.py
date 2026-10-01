"""Runtime-owned short screen impression. Nothing here is written to disk."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

DEFAULT_TTL_SECONDS = 1200.0
STORE_MAX_CHARS = 400
CHAT_MAX_CHARS = 160

_CHAT_INTRO = (
    "【短时屏幕印象】你刚才默默看过对方屏幕的短暂印象（未作为对话说出，不是聊天记录）。"
    "可自然参考；不要主动翻私聊原文，不要当成对方对你说的话："
)
_DIALOGUE_FACT_SPLIT = re.compile(r"(?:対話の既知|对话的已知|対話の既知事実)[：:].*$")


@dataclass(frozen=True)
class SensoryImpression:
    text: str
    updated_at: float
    spoken: bool = False
    window_hint: str = ""
    updated_at_unix: float = 0.0


class SensoryImpressionStore:
    """One rolling impression for this runtime. A later user turn supersedes it."""

    def __init__(self, *, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self._ttl = float(ttl_seconds)
        self._lock = threading.Lock()
        self._current: SensoryImpression | None = None
        self._chat_fact_unix: float | None = None

    def update(
        self,
        text: str,
        *,
        spoken: bool = False,
        window_hint: str = "",
        now: float | None = None,
        wall_unix: float | None = None,
    ) -> None:
        cleaned = (text or "").strip()
        if not cleaned:
            return
        if len(cleaned) > STORE_MAX_CHARS:
            cleaned = cleaned[: STORE_MAX_CHARS - 1] + "…"
        stamp = time.monotonic() if now is None else float(now)
        wall = time.time() if wall_unix is None else float(wall_unix)
        with self._lock:
            self._current = SensoryImpression(
                text=cleaned,
                updated_at=stamp,
                spoken=bool(spoken),
                window_hint=(window_hint or "").strip(),
                updated_at_unix=wall,
            )

    def clear(self) -> None:
        with self._lock:
            self._current = None

    def note_user_fact(self, wall_unix: float | None = None) -> None:
        """A real user turn. Observer decisions must treat older impressions as stale."""
        stamp = time.time() if wall_unix is None else float(wall_unix)
        with self._lock:
            self._chat_fact_unix = stamp

    def get(self, *, now: float | None = None) -> SensoryImpression | None:
        stamp = time.monotonic() if now is None else float(now)
        with self._lock:
            current = self._current
            if current is None:
                return None
            if stamp - current.updated_at > self._ttl:
                self._current = None
                return None
            return current

    def chat_fact_unix(self) -> float | None:
        with self._lock:
            return self._chat_fact_unix

    def superseded_by_user(self, impression: SensoryImpression | None = None) -> bool:
        current = self.get() if impression is None else impression
        fact = self.chat_fact_unix()
        return bool(
            current is not None
            and fact is not None
            and current.updated_at_unix > 0
            and float(fact) > current.updated_at_unix
        )

    def chronology_evidence(self) -> str:
        current = self.get()
        fact = self.chat_fact_unix()
        if current is None or fact is None or current.updated_at_unix <= 0:
            return ""
        if float(fact) <= current.updated_at_unix:
            return ""
        return (
            "[时序] 用户发言 "
            f"unix={float(fact):.0f} 晚于屏幕印象 unix={current.updated_at_unix:.0f}。"
            "早前的屏幕印象不再作为当前情境。"
        )

    def get_for_observer(self, *, now: float | None = None, chat_facts_unix: float | None = None) -> str:
        current = self.get(now=now)
        if current is None:
            return ""
        fact = self.chat_fact_unix() if chat_facts_unix is None else chat_facts_unix
        if (
            fact is not None
            and current.updated_at_unix > 0
            and float(fact) > current.updated_at_unix
        ):
            return ""
        return current.text

    def get_for_chat(self, *, now: float | None = None) -> str:
        current = self.get(now=now)
        if current is None:
            return ""
        return thin_impression_for_chat(current.text, max_chars=CHAT_MAX_CHARS)

    def format_chat_block(self, *, now: float | None = None) -> str:
        body = self.get_for_chat(now=now)
        if not body:
            return ""
        return f"{_CHAT_INTRO}\n{body}"


def thin_impression_for_chat(text: str, *, max_chars: int = CHAT_MAX_CHARS) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    stripped = _DIALOGUE_FACT_SPLIT.sub("", raw).strip().rstrip("。．.；;，,、").strip()
    if not stripped:
        stripped = raw
    if len(stripped) <= max_chars:
        return stripped
    cut = stripped[:max_chars]
    for separator in ("。", "．", ".", "！", "!", "？", "?", "；", ";"):
        index = cut.rfind(separator)
        if index >= max(24, max_chars // 3):
            return cut[: index + 1]
    return cut.rstrip() + "…"
