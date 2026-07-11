"""ProactiveObserver — desktop-kanajo style screen-watching loop.

Polls desktop state (window title, idle time) and periodically evaluates
via a vision LLM whether to speak up unprompted.

Runs in a background thread with its own asyncio event loop, using a
thread-safe Signal for Qt main-thread dispatch.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx
from loguru import logger

from app.perception.privacy import PrivacyGuard
from app.perception.screen_capture import ScreenCapture
from app.perception.win32 import get_active_window_title, get_idle_seconds

OnSpeakFn = Callable[[str, str], "None"]  # (ja_text, zh_text)
IsBusyFn = Callable[[], bool]
OnMemoryRecordFn = Callable[[str], "None"]


# ---------------------------------------------------------------------------
# Proactive evaluation prompt — adapted from desktop-kanojo OpenMeido
# ---------------------------------------------------------------------------

_PROACTIVE_SYSTEM_PROMPT = """你现在是后台运行的"主动模式"。我刚才悄悄看了一眼用户的屏幕，给你看。
你要严格按 JSON 输出，判断要不要插一句话。

判断标准：
- 用户在专心工作（写代码、读文档、写文章）：should_speak=false，别打扰
- 用户在摸鱼、看视频、看新闻、玩游戏：可以评论
- 用户长时间不动：可以关心
- 看到有趣的/错的/奇怪的内容：可以吐槽
- 不确定：选 false，宁静默不烦人

只输出 JSON，不要 markdown 不要解释：
{"should_speak": true|false, "reason": "给我自己看的简短理由", "ja": "日文台词（TTS用，自然口语）", "zh": "中文译文（气泡显示用）"}

ja 是你心里想说的话（日语），zh 是给用户看的中文翻译。两者一一对应，保持角色风格。
"""

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class ProactiveConfig:
    enabled: bool = True
    timer_seconds: float = 480  # 定期检查间隔（8 分钟）
    cooldown_seconds: float = 600  # 两次主动发言最小间隔
    min_silence_after_user: float = 10  # 用户发言后这时间内不触发
    window_switch_enabled: bool = True
    window_switch_cooldown: float = 120  # 窗口切换触发后的冷却
    idle_threshold_seconds: float = 600  # 空闲多久触发
    poll_interval: float = 5.0  # 状态轮询间隔
    max_edge: int = 1024  # 截图最长边像素
    request_timeout: float = 30.0  # VLM API 超时
    eval_temperature: float = 0.7
    max_tokens: int = 1024

    @classmethod
    def from_dict(cls, d: dict | None) -> "ProactiveConfig":
        if not isinstance(d, dict):
            return cls()
        return cls(
            enabled=bool(d.get("enabled", True)),
            timer_seconds=float(d.get("timer_seconds", 600)),
            cooldown_seconds=float(d.get("cooldown_seconds", 600)),
            min_silence_after_user=float(d.get("min_silence_after_user", 30)),
            window_switch_enabled=bool(d.get("window_switch_enabled", True)),
            window_switch_cooldown=float(d.get("window_switch_cooldown", 300)),
            idle_threshold_seconds=float(d.get("idle_threshold_seconds", 600)),
            poll_interval=float(d.get("poll_interval", 5.0)),
            max_edge=int(d.get("max_edge", 1024)),
            request_timeout=float(d.get("request_timeout", 30.0)),
            eval_temperature=float(d.get("eval_temperature", 0.7)),
            max_tokens=int(d.get("max_tokens", 1024)),
        )


# ---------------------------------------------------------------------------
# Observer
# ---------------------------------------------------------------------------


class ProactiveObserver:
    """Watches the desktop and decides — via VLM — whether to speak.

    Lifecycle: start() → running loop → stop()
    Callbacks: on_speak, is_busy, on_memory_record (optional)
    """

    def __init__(
        self,
        *,
        api_base_url: str,
        api_key: str,
        api_model: str,
        system_prompt: str = "",
        config: ProactiveConfig | None = None,
        privacy: PrivacyGuard | None = None,
        on_speak: OnSpeakFn | None = None,
        is_busy: IsBusyFn | None = None,
        on_memory_record: OnMemoryRecordFn | None = None,
    ) -> None:
        self._api_base_url = api_base_url.rstrip("/")
        self._api_key = api_key
        self._api_model = api_model
        self._system_prompt = system_prompt
        self.config = config or ProactiveConfig()
        self.privacy = privacy or PrivacyGuard()

        self.on_speak = on_speak or (lambda _ja, _zh: None)
        self._is_busy = is_busy or (lambda: False)
        self._on_memory_record = on_memory_record or (lambda _: None)

        self.capture = ScreenCapture(max_edge=self.config.max_edge)

        # State
        self._last_proactive_at = 0.0
        self._last_user_at = time.monotonic()
        self._last_window_title = ""
        self._last_timer_check = time.monotonic()
        self._last_eval_at = 0.0
        self._last_window_trigger_at = 0.0
        self._idle_armed = True
        self._input_focused = False

        self._running = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._http: httpx.AsyncClient | None = None

    # ---- state --------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self.config.enabled = bool(value)
        if not value:
            self._idle_armed = True

    def notify_user_spoke(self) -> None:
        self._last_user_at = time.monotonic()
        self._idle_armed = True

    def notify_input_focused(self) -> None:
        self._input_focused = True

    def notify_input_blurred(self) -> None:
        self._input_focused = False
        self._last_user_at = time.monotonic()

    # ---- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()
        logger.info(
            "ProactiveObserver: started (timer={}s, cooldown={}s, idle={}s)",
            self.config.timer_seconds,
            self.config.cooldown_seconds,
            self.config.idle_threshold_seconds,
        )

    def stop(self) -> None:
        self._running = False
        if self._http:
            try:
                # Close in a fire-and-forget coroutine
                async def _close():
                    await self._http.aclose()
                asyncio.run_coroutine_threadsafe(_close(), self._loop) if self._loop else None
            except Exception:
                pass
            self._http = None
        self._loop = None
        logger.info("ProactiveObserver: stopped")

    def _thread_main(self) -> None:
        """Run the asyncio loop in a dedicated daemon thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        except Exception:
            logger.exception("ProactiveObserver: thread crashed")
        finally:
            self._loop.close()
            self._loop = None

    # ---- core loop ----------------------------------------------------------

    async def _run(self) -> None:
        self._http = httpx.AsyncClient(timeout=self.config.request_timeout)
        self._last_window_title = get_active_window_title()
        try:
            while self._running:
                try:
                    await asyncio.sleep(self.config.poll_interval)
                except asyncio.CancelledError:
                    break

                if not self.config.enabled:
                    continue

                try:
                    triggers = self._collect_triggers()
                    if not triggers:
                        continue
                    if self._is_busy():
                        logger.debug("ProactiveObserver: UI busy, skipping")
                        continue
                    logger.info("ProactiveObserver: evaluating, triggers={}", triggers)
                    await self._do_evaluation(triggers)
                except Exception as e:
                    logger.warning("ProactiveObserver loop error: {}", e)
        finally:
            if self._http:
                await self._http.aclose()
                self._http = None

    def _collect_triggers(self) -> list[str]:
        now = time.monotonic()

        # Absolute filters — silence required
        if self._input_focused:
            return []
        if now - self._last_user_at < self.config.min_silence_after_user:
            return []
        if self._last_proactive_at and now - self._last_proactive_at < self.config.cooldown_seconds:
            return []
        if now - self._last_eval_at < self.config.poll_interval * 1.5:
            return []

        triggers: list[str] = []

        if now - self._last_timer_check >= self.config.timer_seconds:
            triggers.append("timer")
            self._last_timer_check = now

        if self.config.window_switch_enabled:
            cur = get_active_window_title()
            if cur and cur != self._last_window_title:
                if now - self._last_window_trigger_at >= self.config.window_switch_cooldown:
                    triggers.append(f"window:{self._last_window_title!r}->{cur!r}")
                    self._last_window_trigger_at = now
                self._last_window_title = cur

        idle = get_idle_seconds()
        if idle >= self.config.idle_threshold_seconds and self._idle_armed:
            triggers.append(f"idle:{int(idle)}s")
            self._idle_armed = False

        return triggers

    async def _do_evaluation(self, triggers: list[str]) -> None:
        now = time.monotonic()
        self._last_eval_at = now

        # Privacy check — must happen BEFORE screenshot
        blocked, matched = self.privacy.check_active_window()
        if blocked:
            logger.info("ProactiveObserver: privacy block ({})", matched)
            return

        # Capture screen
        try:
            obs = self.capture.grab()
        except Exception as e:
            logger.warning("ProactiveObserver: screen capture failed: {}", e)
            return

        # Build context
        window_title = get_active_window_title()
        idle_s = int(get_idle_seconds())

        ctx_parts = []
        if window_title:
            ctx_parts.append(f"活动窗口：{window_title}")
        if idle_s >= 60:
            ctx_parts.append(f"距离最后输入：{idle_s // 60} 分 {idle_s % 60} 秒")
        elif idle_s > 0:
            ctx_parts.append(f"距离最后输入：{idle_s} 秒")
        if triggers:
            ctx_parts.append(f"触发原因：{', '.join(triggers)}")
        ctx_text = "\n".join(ctx_parts) or "（无额外上下文）"

        user_text = f"{ctx_text}\n\n（截图见下）"

        # Build messages
        messages = [
            {"role": "system", "content": [{"type": "text", "text": self._build_full_system_prompt()}]},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{obs.mime};base64,{obs.image_b64}"},
                    },
                ],
            },
        ]

        # Call VLM
        vlm_start = time.monotonic()
        logger.debug("ProactiveObserver: VLM call start (timeout={}s, triggers={})",
                     self.config.request_timeout, triggers)
        try:
            response = await asyncio.wait_for(
                self._chat_completion(messages),
                timeout=self.config.request_timeout + 15,
            )
            vlm_elapsed = time.monotonic() - vlm_start
            logger.debug("ProactiveObserver: VLM call done ({:.1f}s, {} chars)",
                         vlm_elapsed, len(response or ""))
        except asyncio.TimeoutError:
            logger.warning("ProactiveObserver: VLM call timed out after {:.0f}s",
                           time.monotonic() - vlm_start)
            return
        except Exception as e:
            logger.warning("ProactiveObserver: VLM call failed: {}", e)
            # 代理切换等会破坏 httpcore 连接池，重建一次
            try:
                old = self._http
                self._http = httpx.AsyncClient(timeout=self.config.request_timeout)
                if old:
                    await old.aclose()
            except Exception:
                pass
            return

        parsed = _extract_json(response)
        if not parsed:
            logger.warning("ProactiveObserver: no JSON in response: {!r}", response[:200])
            return

        if not parsed.get("should_speak"):
            logger.debug("ProactiveObserver: silent (reason: {})", parsed.get("reason", ""))
            return

        # Support old "comment" field as fallback
        ja_text = str(parsed.get("ja", "") or parsed.get("comment", "")).strip()
        zh_text = str(parsed.get("zh", "")).strip()
        if not ja_text:
            return
        if not zh_text:
            zh_text = ja_text  # 模型没给 zh 时用 ja 兜底

        self._last_proactive_at = time.monotonic()
        self._idle_armed = True

        # Deliver to UI (ja for TTS, zh for subtitle)
        try:
            self.on_speak(ja_text, zh_text)
        except Exception as e:
            logger.warning("ProactiveObserver: on_speak callback error: {}", e)

        # Save to memory (bilingual)
        try:
            self._on_memory_record(f"[主动] {ja_text} | {zh_text}")
        except Exception as e:
            logger.warning("ProactiveObserver: memory record error: {}", e)

    def _build_full_system_prompt(self) -> str:
        parts = []
        if self._system_prompt.strip():
            parts.append(self._system_prompt.strip())
        parts.append(_PROACTIVE_SYSTEM_PROMPT)
        return "\n\n---\n\n".join(parts)

    async def _chat_completion(self, messages: list[dict]) -> str:
        """Simple non-streaming chat completion via httpx."""
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.config.request_timeout)

        url = f"{self._api_base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._api_model,
            "messages": messages,
            "temperature": self.config.eval_temperature,
            "max_tokens": self.config.max_tokens,
        }

        resp = await self._http.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        choice = data.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content")
        finish = choice.get("finish_reason", "")
        if not content:
            logger.warning(
                "ProactiveObserver: VLM returned empty content (finish={}, model={})",
                finish, self._api_model,
            )
            logger.debug("ProactiveObserver: raw response: {}", json.dumps(data, ensure_ascii=False)[:500])
        return content or ""


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try fenced code block
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    # Try bare JSON object
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None
