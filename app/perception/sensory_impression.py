"""短时屏幕印象 — Observer 与主对话共享的薄状态。

不说不进聊天历史；只在内存里保留一条会过期的场景印象：
- 下次 Observer 评估（VLM）读较完整版
- 下次主对话读截断版，控制 token
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

# 20 分钟：介于「刚看过」与「早就过时」之间
DEFAULT_TTL_SECONDS = 1200.0
# 存盘/给 VLM 的上限（situational_summary 本就 2〜4 句）
STORE_MAX_CHARS = 400
# 主对话注入硬顶，避免每轮背上长摘要
CHAT_MAX_CHARS = 160

_CHAT_INTRO = (
    "【短时屏幕印象】你刚才默默看过对方屏幕的短暂印象（未作为对话说出，不是聊天记录）。"
    "可自然参考；不要主动翻私聊原文，不要当成对方对你说的话："
)

_DIALOGUE_FACT_SPLIT = re.compile(r"(?:対話の既知|对话的已知|対話の既知事実)[：:].*$")


@dataclass(frozen=True)
class SensoryImpression:
    text: str
    updated_at: float  # time.monotonic()
    spoken: bool = False
    window_hint: str = ""


class SensoryImpressionStore:
    """进程内单条滚动印象；线程安全。"""

    def __init__(self, *, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self._ttl = float(ttl_seconds)
        self._lock = threading.Lock()
        self._current: SensoryImpression | None = None

    def update(
        self,
        text: str,
        *,
        spoken: bool = False,
        window_hint: str = "",
        now: float | None = None,
    ) -> None:
        cleaned = (text or "").strip()
        if not cleaned:
            return
        if len(cleaned) > STORE_MAX_CHARS:
            cleaned = cleaned[: STORE_MAX_CHARS - 1] + "…"
        stamp = time.monotonic() if now is None else float(now)
        with self._lock:
            self._current = SensoryImpression(
                text=cleaned,
                updated_at=stamp,
                spoken=bool(spoken),
                window_hint=(window_hint or "").strip(),
            )

    def clear(self) -> None:
        with self._lock:
            self._current = None

    def get(
        self,
        *,
        now: float | None = None,
    ) -> SensoryImpression | None:
        stamp = time.monotonic() if now is None else float(now)
        with self._lock:
            cur = self._current
            if cur is None:
                return None
            if stamp - cur.updated_at > self._ttl:
                self._current = None
                return None
            return cur

    def get_for_observer(self, *, now: float | None = None) -> str:
        """给 VLM 的观察者上下文（完整存档文）。"""
        cur = self.get(now=now)
        return cur.text if cur else ""

    def get_for_chat(self, *, now: float | None = None) -> str:
        """给主对话的薄注入正文（不含 intro）。"""
        cur = self.get(now=now)
        if cur is None:
            return ""
        return thin_impression_for_chat(cur.text, max_chars=CHAT_MAX_CHARS)

    def format_chat_block(self, *, now: float | None = None) -> str:
        body = self.get_for_chat(now=now)
        if not body:
            return ""
        return f"{_CHAT_INTRO}\n{body}"


def thin_impression_for_chat(text: str, *, max_chars: int = CHAT_MAX_CHARS) -> str:
    """去掉「对话已知」尾巴，压到短场景句，控制主对话 token。"""
    raw = (text or "").strip()
    if not raw:
        return ""
    # 对话事实主对话已有历史，这里只留画面场景
    stripped = _DIALOGUE_FACT_SPLIT.sub("", raw).strip()
    stripped = stripped.rstrip("。．.；;，,、").strip()
    if not stripped:
        stripped = raw
    if len(stripped) <= max_chars:
        return stripped
    # 尽量在句号处截断
    cut = stripped[:max_chars]
    for sep in ("。", "．", ".", "！", "!", "？", "?", "；", ";"):
        idx = cut.rfind(sep)
        if idx >= max(24, max_chars // 3):
            return cut[: idx + 1]
    return cut.rstrip() + "…"


# 进程级单例：Observer 写、主对话读
sensory_impression_store = SensoryImpressionStore()
