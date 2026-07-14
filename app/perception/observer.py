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
from app.core.debug_log import debug_log


def _observer_gui_log(message: str, data: Any | None = None) -> None:
    try:
        debug_log("ProactiveObserver", message, data)
    except Exception:
        pass

@dataclass(frozen=True)
class ProactiveSpeakPayload:
    """主动发言内容；comment 为角色口吻台词，translation/tone 可选。"""

    text: str
    translation: str = ""
    tone: str = "中性"


OnSpeakFn = Callable[[ProactiveSpeakPayload], "None"]
IsBusyFn = Callable[[], bool]
OnMemoryRecordFn = Callable[[str], "None"]


# ---------------------------------------------------------------------------
# Proactive evaluation prompt — adapted from desktop-kanojo OpenMeido
# ---------------------------------------------------------------------------

_PROACTIVE_SYSTEM_PROMPT = """你现在是后台运行的"主动模式"。我刚才悄悄看了一眼用户的屏幕，给你看。
你要严格按 JSON 输出，判断要不要插一句话，以及建议下次多久后再看。

判断标准：
- 用户在专心工作（写代码、读文档、写文章）：should_speak=false，别打扰
- 用户在摸鱼、看视频、看新闻、玩游戏：可以评论
- 用户长时间不动：可以关心
- 看到有趣的/错的/奇怪的内容：可以吐槽
- 不确定：选 false，宁静默不烦人

suggested_interval 是你觉得下次看屏幕之前的等待秒数：
- 用户很专注（写代码/文档/开会）：建议长一点（600-1800 秒）
- 用户在休闲（浏览/看视频）：建议短一点（120-480 秒）
- 默认/不确定：480 秒
- 有效范围 120-1800 秒

只输出 JSON，不要 markdown 不要解释：
{"should_speak": true|false, "suggested_interval": 480, "reason": "给我自己看的简短理由", "comment": "对用户说的日文台词，仅当 should_speak=true 时填", "translation": "comment 的中文译文（可选，无则空字符串）", "tone": "中性|不满|害羞|请求|困惑|惊讶 之一，可选，默认中性"}

comment 必须保持你的角色风格：短句、口语、自然、不打破角色设定，用日文。
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
    # 自适应间隔范围（秒）
    adaptive_interval_min: float = 120.0
    adaptive_interval_max: float = 1800.0
    # away_mode 最大持续时间（秒），超时自动恢复
    away_max_seconds: float = 12 * 3600

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
            adaptive_interval_min=float(d.get("adaptive_interval_min", 120.0)),
            adaptive_interval_max=float(d.get("adaptive_interval_max", 1800.0)),
            away_max_seconds=float(d.get("away_max_seconds", 12 * 3600)),
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

        self.on_speak = on_speak or (lambda _payload: None)
        self._is_busy = is_busy or (lambda: False)
        self._on_memory_record = on_memory_record or (lambda _: None)

        self.capture = ScreenCapture(max_edge=self.config.max_edge)

        # State
        self._last_proactive_at = 0.0
        self._last_user_at = time.monotonic()
        self._last_window_title = ""
        self._last_timer_check = time.monotonic()
        self._next_timer_at: float = 0.0  # 自适应下次检查时间
        self._last_eval_at = 0.0
        self._last_window_trigger_at = 0.0
        self._idle_armed = True

        # Away mode（用户明确告知离开时暂停感知）
        self._away_mode: bool = False
        self._away_set_at: float = 0.0

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
        # 用户发消息 → 自动退出 away_mode
        if self._away_mode:
            self.set_away_mode(False)
            logger.info("ProactiveObserver: away_mode cleared by user message")
            _observer_gui_log("用户归来，away_mode 自动解除")

    # ---- away mode -----------------------------------------------------------

    def set_away_mode(self, value: bool) -> None:
        """设置离开模式：True=暂停感知，False=恢复正常。"""
        self._away_mode = bool(value)
        self._away_set_at = time.monotonic() if value else 0.0
        if value:
            self._idle_armed = True
            self._next_timer_at = 0.0  # 重置自适应计时器
            logger.info("ProactiveObserver: away_mode ON")
            _observer_gui_log("away_mode 已开启")
        else:
            logger.info("ProactiveObserver: away_mode OFF")
            _observer_gui_log("away_mode 已关闭")

    @property
    def away_mode(self) -> bool:
        return self._away_mode

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
        _observer_gui_log(
            "主动观察已启动",
            {
                "timer_seconds": self.config.timer_seconds,
                "cooldown_seconds": self.config.cooldown_seconds,
                "idle_threshold_seconds": self.config.idle_threshold_seconds,
            },
        )

    def stop(self) -> None:
        self._running = False
        loop = self._loop
        if loop is not None:
            # Cancel all pending tasks so the loop exits promptly instead of
            # blocking on asyncio.sleep() or a mid-flight VLM request.
            try:
                for task in asyncio.all_tasks(loop):
                    task.cancel()
            except RuntimeError:
                pass  # loop already closed
        # Thread is daemon; do not join.  Task cancellation and the finally
        # block in _run() handle http cleanup.
        self._http = None
        self._thread = None
        logger.info("ProactiveObserver: stopped")
        _observer_gui_log("主动观察已停止")

    def _thread_main(self) -> None:
        """Run the asyncio loop in a dedicated daemon thread."""
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run())
        except asyncio.CancelledError:
            pass  # expected during shutdown; thread exits cleanly
        except Exception:
            logger.exception("ProactiveObserver: thread crashed")
            _observer_gui_log("主动观察线程异常退出")
        finally:
            try:
                loop.close()
            except Exception:
                pass
            if self._loop is loop:
                self._loop = None
            if self._thread is threading.current_thread():
                self._thread = None

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
                    _observer_gui_log("正在评估是否发言", {"triggers": triggers})
                    await self._do_evaluation(triggers)
                except Exception as e:
                    logger.warning("ProactiveObserver loop error: {}", e)
                    _observer_gui_log("主动观察循环异常", {"error": str(e)})
        finally:
            if self._http:
                try:
                    await asyncio.wait_for(self._http.aclose(), timeout=1.0)
                except (asyncio.TimeoutError, Exception):
                    pass
                self._http = None

    def _collect_triggers(self) -> list[str]:
        now = time.monotonic()

        # Away mode check — hard silence when user explicitly said they're away
        if self._away_mode:
            away_elapsed = now - self._away_set_at
            if away_elapsed >= self.config.away_max_seconds:
                # 超时自动恢复
                self.set_away_mode(False)
                logger.info("ProactiveObserver: away_mode auto-expired after {:.0f}s", away_elapsed)
                _observer_gui_log("away_mode 超时自动恢复")
            else:
                return []  # 完全静默，不截图不评估

        # Absolute filters — silence required
        if now - self._last_user_at < self.config.min_silence_after_user:
            return []
        if self._last_proactive_at and now - self._last_proactive_at < self.config.cooldown_seconds:
            return []
        if now - self._last_eval_at < self.config.poll_interval * 1.5:
            return []

        triggers: list[str] = []

        # Adaptive timer: use LLM-suggested interval if available, else fallback to fixed
        timer_target = self._next_timer_at if self._next_timer_at > 0 else (self._last_timer_check + self.config.timer_seconds)
        if now >= timer_target:
            triggers.append("timer")
            self._last_timer_check = now
            self._next_timer_at = 0.0  # reset, will be set again after evaluation

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
            _observer_gui_log("隐私拦截", {"matched": matched})
            return

        # Capture screen
        try:
            obs = self.capture.grab()
        except Exception as e:
            logger.warning("ProactiveObserver: screen capture failed: {}", e)
            _observer_gui_log("截图失败", {"error": str(e)})
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
        try:
            response = await self._chat_completion(messages)
        except Exception as e:
            logger.warning("ProactiveObserver: VLM call failed: {}", e)
            _observer_gui_log("VLM 调用失败", {"error": str(e)})
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

        # Adaptive interval: use LLM-suggested value, clamped to safe range
        suggested = parsed.get("suggested_interval")
        if isinstance(suggested, (int, float)) and suggested > 0:
            clamped = max(self.config.adaptive_interval_min,
                          min(float(suggested), self.config.adaptive_interval_max))
            self._next_timer_at = time.monotonic() + clamped
            logger.debug("ProactiveObserver: adaptive interval set to {:.0f}s (requested {:.0f}s)", clamped, suggested)
        else:
            self._next_timer_at = 0.0  # fallback to default timer

        if not parsed.get("should_speak"):
            logger.debug("ProactiveObserver: silent (reason: {})", parsed.get("reason", ""))
            return

        comment = str(parsed.get("comment", "")).strip()
        if not comment:
            return

        self._last_proactive_at = time.monotonic()
        self._idle_armed = True

        payload = ProactiveSpeakPayload(
            text=comment,
            translation=str(parsed.get("translation", "")).strip(),
            tone=str(parsed.get("tone", "")).strip() or "中性",
        )

        # Deliver to UI（正式回复管线由 PetWindow 负责：字幕/TTS/历史/立绘）
        try:
            self.on_speak(payload)
        except Exception as e:
            logger.warning("ProactiveObserver: on_speak callback error: {}", e)
            _observer_gui_log("主动发言回调失败", {"error": str(e)})

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
