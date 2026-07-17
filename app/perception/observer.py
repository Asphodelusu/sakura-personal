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
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

import httpx
from loguru import logger

from app.core.debug_log import debug_log
from app.perception.privacy import PrivacyGuard
from app.perception.screen_capture import ScreenCapture
from app.perception.screen_reader import WindowText, read_active_window
from app.perception.win32 import (
    get_active_window_process_name,
    get_active_window_title,
    get_foreground_hwnd,
    get_idle_seconds,
)


def _observer_gui_log(message: str, data: Any | None = None) -> None:
    try:
        debug_log("ProactiveObserver", message, data)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Thread-isolated UIA reader — prevents SEH crashes in uiautomation native
# code from killing the observer thread.
# ---------------------------------------------------------------------------

_UIA_ISOLATE_TIMEOUT = 3.0
_OCR_ISOLATE_TIMEOUT = 8.0
_COINIT_APARTMENTTHREADED = 0x2


def _read_window_text_isolated() -> WindowText:
    """Call read_active_window() in a dedicated thread with COM init."""
    import ctypes as _ctypes
    import queue as _queue

    result_queue: _queue.Queue[WindowText] = _queue.Queue(maxsize=1)

    def _worker() -> None:
        _ctypes.windll.ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
        try:
            result_queue.put(read_active_window())
        except Exception:
            result_queue.put(WindowText())
        finally:
            _ctypes.windll.ole32.CoUninitialize()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    try:
        return result_queue.get(timeout=_UIA_ISOLATE_TIMEOUT)
    except _queue.Empty:
        logger.warning(
            "ProactiveObserver: UIA read timed out after {:.0f}s",
            _UIA_ISOLATE_TIMEOUT,
        )
        return WindowText()
    finally:
        t.join(timeout=0.5)


def _ocr_game_dialogue_isolated() -> str:
    """OCR the bottom third of the focused window in an isolated thread.

    WinRT awaits can hang or crash the observer loop; keep blast radius contained.
    """
    import ctypes as _ctypes
    import queue as _queue

    result_queue: _queue.Queue[str] = _queue.Queue(maxsize=1)

    def _worker() -> None:
        _ctypes.windll.ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            result_queue.put(loop.run_until_complete(_ocr_game_dialogue_async()))
        except Exception as e:
            logger.debug("ProactiveObserver: game OCR worker failed: {}", e)
            result_queue.put("")
        finally:
            try:
                loop.close()
            except Exception:
                pass
            _ctypes.windll.ole32.CoUninitialize()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    try:
        return result_queue.get(timeout=_OCR_ISOLATE_TIMEOUT)
    except _queue.Empty:
        logger.warning(
            "ProactiveObserver: game OCR timed out after {:.0f}s",
            _OCR_ISOLATE_TIMEOUT,
        )
        return ""
    finally:
        t.join(timeout=0.5)


async def _ocr_game_dialogue_async() -> str:
    """OCR focus-window bottom ~1/3 (common dialogue / subtitle region)."""
    tmp_path = ""
    try:
        from ctypes import byref, windll
        from ctypes.wintypes import RECT
        import os as _os
        import tempfile as _tempfile

        import mss as _mss
        from PIL import Image as _Image
        from winsdk.windows.graphics.imaging import BitmapDecoder
        from winsdk.windows.media.ocr import OcrEngine
        from winsdk.windows.storage import StorageFile
        from winsdk.windows.storage.streams import RandomAccessStreamReference

        hwnd = windll.user32.GetForegroundWindow()
        if not hwnd:
            return ""
        rect = RECT()
        windll.user32.GetWindowRect(hwnd, byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w < 200 or h < 200:
            return ""

        bottom_h = max(80, h // 3)
        mon = {
            "left": rect.left,
            "top": rect.top + h - bottom_h,
            "width": w,
            "height": bottom_h,
        }
        with _mss.MSS() as sct:
            raw = sct.grab(mon)
        img = _Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

        tmp = _tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp_path = tmp.name
        img.save(tmp_path, format="PNG")
        tmp.close()

        file = await StorageFile.get_file_from_path_async(tmp_path)
        stream_ref = RandomAccessStreamReference.create_from_file(file)
        stream = await stream_ref.open_read_async()
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            return ""
        result = await engine.recognize_async(bitmap)
        return (result.text or "").strip()
    except ImportError:
        return ""
    except Exception as e:
        logger.debug("ProactiveObserver: game OCR failed: {}", e)
        return ""
    finally:
        if tmp_path:
            try:
                import os as _os

                _os.unlink(tmp_path)
            except OSError:
                pass


@dataclass(frozen=True)
class ProactiveSpeakPayload:
    """主动发言内容；comment 为角色口吻台词，translation/tone 可选。"""

    text: str
    translation: str = ""
    tone: str = "中性"


OnSpeakFn = Callable[[ProactiveSpeakPayload], None]
OnEvaluateFn = Callable[[str, bool], None]
IsBusyFn = Callable[[], Any]
OnMemoryRecordFn = Callable[[str], None]


_PROACTIVE_SYSTEM_PROMPT = """你现在是后台运行的"主动模式"。消息里可能有：
- 截图（传统方式）
- [UIA 直接读取] 标记的文字（系统无障碍接口直接读的窗口内容，不需要 OCR）
- [OCR 游戏文本] 标记的文字（判定为游戏/自绘界面时，对对话框区域 OCR 的结果；可能有识别误差）

你要严格按 JSON 输出，判断要不要插一句话，以及建议下次多久后再看。

判断标准：
- 相手が集中して作業している（コード・文書・執筆）：should_speak=false、邪魔しない
- 相手が息抜きしている（動画・ニュース・ゲーム）：コメントしてもいい
- 相手が長く動かない：気遣ってもいい
- 面白い／間違ってる／変なものを見つけた：ツッコミを入れてもいい
- UIA / OCR で読んだ文字とスクリーンショットは相互に裏付けを取ること；文字から何のアプリ・何のページ・何のセリフか判断できる
- 迷ったら false、静かに見守るほうが煩わしくない

suggested_interval は次に画面を見るまでの待機秒数：
- 相手が集中している（コード・文書・会議）：長め（600-1800 秒）
- 相手がリラックスしている（ブラウジング・動画）：短め（45-120 秒）
- デフォルト／わからない：480 秒
- 有効範囲 45-1800 秒

只输出 JSON，不要 markdown 不要解释：
{"should_speak": true|false, "suggested_interval": 480, "reason": "给我自己看的简短理由", "comment": "相手に話しかけるセリフ、should_speak=true のときだけ", "translation": "comment 的中文译文（可选，无则空字符串）", "tone": "中性|不满|害羞|请求|困惑|惊讶 之一，可选，默认中性"}

comment 必须保持你的角色风格：短句、口语、自然、不打破角色设定，用日文。
一言か二言まで。長文厳禁。どうしても言いたいことが多い時は、一番大事なことだけに絞って。
"""

_NON_GAME_PROCESSES = frozenset({
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
    "code.exe", "cursor.exe", "devenv.exe", "notepad.exe", "notepad++.exe",
    "explorer.exe", "windowsterminal.exe", "cmd.exe", "powershell.exe",
    "pwsh.exe", "discord.exe", "slack.exe", "teams.exe", "outlook.exe",
    "winword.exe", "excel.exe", "powerpnt.exe", "wechat.exe", "weixin.exe",
    "qq.exe", "telegram.exe", "spotify.exe", "obs64.exe", "obs32.exe",
})
_GAME_PROCESS_HINTS = (
    "unity", "unreal", "ue4", "ue5", "godot", "gamemaker",
    "krkr", "kiri", "renpy", "rpg_", "rpgmaker", "nw.exe",
    "game", "galgame", "siglus", "bgi.exe", "yuris",
)


@dataclass
class ProactiveConfig:
    enabled: bool = True
    timer_seconds: float = 480
    cooldown_seconds: float = 600
    min_silence_after_user: float = 10
    window_switch_enabled: bool = True
    # APP_FOCUS 类型冷却（对齐同类 ~20s；冷却内切换记 deferred，结束后补票）
    window_switch_cooldown: float = 25
    # 前台稳定多久才算一次切应用触发（快切会不断重置）
    focus_settle_delay: float = 5
    idle_threshold_seconds: float = 600
    poll_interval: float = 5.0
    content_check_interval: float = 30.0
    content_min_chars: int = 30
    # 暂时默认关闭：WinRT OCR 在游戏窗上常超时(~8s)，拖慢评估且收益不稳。
    game_ocr_enabled: bool = False
    max_edge: int = 1920
    request_timeout: float = 60.0
    eval_temperature: float = 0.7
    max_tokens: int = 1024
    adaptive_interval_min: float = 45.0
    adaptive_interval_max: float = 1800.0
    away_max_seconds: float = 12 * 3600

    @classmethod
    def from_dict(cls, d: dict | None) -> "ProactiveConfig":
        if not isinstance(d, dict):
            return cls()
        base = cls()
        return cls(
            enabled=bool(d.get("enabled", base.enabled)),
            timer_seconds=float(d.get("timer_seconds", base.timer_seconds)),
            cooldown_seconds=float(d.get("cooldown_seconds", base.cooldown_seconds)),
            min_silence_after_user=float(
                d.get("min_silence_after_user", base.min_silence_after_user)
            ),
            window_switch_enabled=bool(
                d.get("window_switch_enabled", base.window_switch_enabled)
            ),
            window_switch_cooldown=float(
                d.get("window_switch_cooldown", base.window_switch_cooldown)
            ),
            focus_settle_delay=float(d.get("focus_settle_delay", base.focus_settle_delay)),
            idle_threshold_seconds=float(
                d.get("idle_threshold_seconds", base.idle_threshold_seconds)
            ),
            poll_interval=float(d.get("poll_interval", base.poll_interval)),
            content_check_interval=float(
                d.get("content_check_interval", base.content_check_interval)
            ),
            content_min_chars=int(d.get("content_min_chars", base.content_min_chars)),
            game_ocr_enabled=bool(d.get("game_ocr_enabled", base.game_ocr_enabled)),
            max_edge=int(d.get("max_edge", base.max_edge)),
            request_timeout=float(d.get("request_timeout", base.request_timeout)),
            eval_temperature=float(d.get("eval_temperature", base.eval_temperature)),
            max_tokens=int(d.get("max_tokens", base.max_tokens)),
            adaptive_interval_min=float(
                d.get("adaptive_interval_min", base.adaptive_interval_min)
            ),
            adaptive_interval_max=float(
                d.get("adaptive_interval_max", base.adaptive_interval_max)
            ),
            away_max_seconds=float(d.get("away_max_seconds", base.away_max_seconds)),
        )


@dataclass(frozen=True)
class FocusSnapshot:
    """Foreground identity: process+HWND = APP_FOCUS; title is display only."""

    hwnd: int
    process: str
    title: str
    changed_at: float = 0.0

    @property
    def app_key(self) -> str:
        proc = (self.process or "").casefold()
        return f"{proc}|{int(self.hwnd)}"

    @property
    def label(self) -> str:
        return self.title or self.process or f"hwnd:{self.hwnd}"


@dataclass
class ObservationRecord:
    """A single observation evaluated by the VLM."""

    timestamp: float
    window_title: str
    should_speak: bool
    reason: str
    comment: str = ""


class ProactiveObserver:
    """Watches the desktop and decides — via VLM — whether to speak."""

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
        on_evaluate: OnEvaluateFn | None = None,
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
        self.on_evaluate = on_evaluate or (lambda _reason, _should_speak: None)
        self._is_busy = is_busy or (lambda: False)
        self._on_memory_record = on_memory_record or (lambda _: None)
        self._get_recent_history: Callable[[], str] = lambda: ""
        self._obs_history: deque[ObservationRecord] = deque(maxlen=5)

        self.capture = ScreenCapture(max_edge=self.config.max_edge)

        self._last_proactive_at = 0.0
        self._last_user_at = time.monotonic()
        # 兼容旧日志字段：始终等于当前焦点标题
        self._last_window_title = ""
        self._focus_current: FocusSnapshot | None = None
        self._focus_previous: FocusSnapshot | None = None
        self._pending_focus: FocusSnapshot | None = None
        self._focus_settled_at: float = 0.0
        self._deferred_focus: FocusSnapshot | None = None
        self._ready_focus_trigger: str = ""
        self._last_timer_check = time.monotonic()
        self._next_timer_at: float = 0.0
        self._last_eval_at = 0.0
        self._last_window_trigger_at = 0.0
        self._idle_armed = True

        self._last_frame_dhash: int | None = None
        self._last_dedup_skip_at: float = 0.0

        self._last_text_hash: int | None = None
        self._last_content_check_at: float = 0.0
        self._cached_window_text: WindowText | None = None
        self._cached_window_text_at: float = 0.0
        self._cached_window_title: str = ""

        self._away_mode: bool = False
        self._away_set_at: float = 0.0

        self._running = False
        self._run_epoch = 0
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._http: httpx.AsyncClient | None = None
        self._last_busy_log_at: float = 0.0
        self._was_busy = False
        self._last_busy_reason = ""

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self.config.enabled = bool(value)
        if not value:
            self._idle_armed = True

    def set_recent_history_provider(self, provider: Callable[[], str]) -> None:
        """Set a callback that returns recent conversation history as a string."""
        self._get_recent_history = provider

    def notify_user_spoke(self) -> None:
        self._last_user_at = time.monotonic()
        self._idle_armed = True
        if self._away_mode:
            self.set_away_mode(False)
            logger.info("ProactiveObserver: away_mode cleared by user message")
            _observer_gui_log("away_mode 自動解除")

    def set_away_mode(self, value: bool) -> None:
        self._away_mode = bool(value)
        self._away_set_at = time.monotonic() if value else 0.0
        if value:
            self._idle_armed = True
            self._next_timer_at = 0.0
            logger.info("ProactiveObserver: away_mode ON")
            _observer_gui_log("away_mode 已开启")
        else:
            logger.info("ProactiveObserver: away_mode OFF")
            _observer_gui_log("away_mode 已关闭")

    @property
    def away_mode(self) -> bool:
        return self._away_mode

    def start(self) -> None:
        if self._running:
            return
        # 旧线程若仍在收尾（崩溃/stop 竞态），先等一下避免双循环。
        old = self._thread
        if old is not None and old.is_alive() and old is not threading.current_thread():
            old.join(timeout=2.0)
        self._run_epoch += 1
        epoch = self._run_epoch
        self._running = True
        self._thread = threading.Thread(
            target=self._thread_main,
            args=(epoch,),
            daemon=True,
            name="ProactiveObserver",
        )
        self._thread.start()
        logger.info(
            "ProactiveObserver: started (timer={}s, cooldown={}s, idle={}s, model={})",
            self.config.timer_seconds,
            self.config.cooldown_seconds,
            self.config.idle_threshold_seconds,
            self._api_model,
        )
        _observer_gui_log(
            "主动观察已启动",
            {
                "timer_seconds": self.config.timer_seconds,
                "cooldown_seconds": self.config.cooldown_seconds,
                "idle_threshold_seconds": self.config.idle_threshold_seconds,
                "model": self._api_model,
                "base_url": self._api_base_url,
            },
        )

    def stop(self) -> None:
        self._running = False
        thread = self._thread
        loop = self._loop
        if loop is not None:
            try:
                def _cancel_all() -> None:
                    for task in asyncio.all_tasks(loop):
                        task.cancel()

                loop.call_soon_threadsafe(_cancel_all)
            except RuntimeError:
                pass
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._http = None
        if self._thread is thread:
            self._thread = None
        logger.info("ProactiveObserver: stopped")
        _observer_gui_log("主动观察已停止")

    def _thread_main(self, epoch: int) -> None:
        import ctypes as _ctypes

        _com_initialized = (
            _ctypes.windll.ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED) == 0
        )
        try:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._run())
            except asyncio.CancelledError:
                pass
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
        finally:
            if _com_initialized:
                try:
                    _ctypes.windll.ole32.CoUninitialize()
                except Exception:
                    pass
            # 仅本代线程清除闸门，避免 stop→start 竞态把新循环的 _running 打回 False。
            if self._run_epoch == epoch:
                self._running = False
                if self._thread is threading.current_thread():
                    self._thread = None

    async def _run(self) -> None:
        self._http = httpx.AsyncClient(timeout=self.config.request_timeout)
        self._sync_focus_tracking(time.monotonic(), seed_only=True)
        try:
            while self._running:
                try:
                    await asyncio.sleep(self.config.poll_interval)
                except asyncio.CancelledError:
                    break

                if not self.config.enabled:
                    continue

                try:
                    now = time.monotonic()
                    # busy / 冷却期间也持续跟踪焦点，避免丢切换、错计时。
                    self._sync_focus_tracking(now)

                    busy = self._is_busy()
                    if busy:
                        reason = busy if isinstance(busy, str) else "busy"
                        self._was_busy = True
                        self._last_busy_reason = reason
                        now_busy = time.monotonic()
                        if now_busy - self._last_busy_log_at >= 60.0:
                            self._last_busy_log_at = now_busy
                            logger.info(
                                "ProactiveObserver: UI busy, holding triggers ({})",
                                reason,
                            )
                            _observer_gui_log(
                                "UI 忙碌，暂缓评估（不消耗触发）",
                                {"reason": reason},
                            )
                        continue
                    if self._was_busy:
                        self._was_busy = False
                        logger.info(
                            "ProactiveObserver: UI idle, resuming (was: {})",
                            self._last_busy_reason,
                        )
                    triggers = await self._collect_triggers()
                    if not triggers:
                        continue
                    self._consume_focus_triggers(triggers)
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

    def _read_focus_snapshot(self, *, now: float | None = None) -> FocusSnapshot | None:
        hwnd = int(get_foreground_hwnd() or 0)
        title = get_active_window_title()
        process = get_active_window_process_name()
        if hwnd <= 0 and not title and not process:
            return None
        return FocusSnapshot(
            hwnd=hwnd,
            process=process or "",
            title=title or "",
            changed_at=time.monotonic() if now is None else now,
        )

    def _arm_focus_settle(self, snap: FocusSnapshot, *, now: float) -> None:
        """开始/重置 settle；快切时不断重置截止时间。"""
        self._pending_focus = snap
        self._focus_settled_at = now + self.config.focus_settle_delay
        self._deferred_focus = None

    def _defer_focus(self, snap: FocusSnapshot) -> None:
        """类型冷却中：记下脏焦点，冷却结束后补票。"""
        self._deferred_focus = snap
        self._pending_focus = None
        self._focus_settled_at = 0.0

    def _promote_deferred_focus(self, *, now: float) -> None:
        deferred = self._deferred_focus
        current = self._focus_current
        if deferred is None or current is None:
            return
        if deferred.app_key != current.app_key:
            return
        if now - self._last_window_trigger_at < self.config.window_switch_cooldown:
            return
        # 冷却期已在前台稳住的时间可计入 settle
        elapsed = max(0.0, now - deferred.changed_at)
        settle = self.config.focus_settle_delay
        if elapsed >= settle:
            self._emit_ready_focus_trigger(previous=self._focus_previous, current=current, now=now)
            self._deferred_focus = None
            self._pending_focus = None
            self._focus_settled_at = 0.0
        else:
            self._pending_focus = current
            self._focus_settled_at = deferred.changed_at + settle
            self._deferred_focus = None

    def _emit_ready_focus_trigger(
        self,
        *,
        previous: FocusSnapshot | None,
        current: FocusSnapshot,
        now: float,
    ) -> None:
        from_label = previous.label if previous is not None else "(unknown)"
        self._ready_focus_trigger = f"window:{from_label!r}->{current.label!r}"
        self._last_window_trigger_at = now

    def _sync_focus_tracking(self, now: float, *, seed_only: bool = False) -> None:
        """始终跟踪前台 APP_FOCUS（进程+HWND）。busy/冷却也调用，保证不丢切换。"""
        if not self.config.window_switch_enabled and not seed_only:
            return

        snap = self._read_focus_snapshot(now=now)
        if snap is None:
            return

        self._last_window_title = snap.title

        if self._focus_current is None or seed_only:
            self._focus_current = snap
            self._last_window_title = snap.title
            return

        current = self._focus_current
        if snap.app_key == current.app_key:
            # 同应用仅标题变化：更新展示名，不重置 APP_FOCUS settle
            if snap.title != current.title or snap.process != current.process:
                self._focus_current = FocusSnapshot(
                    hwnd=current.hwnd,
                    process=snap.process or current.process,
                    title=snap.title or current.title,
                    changed_at=current.changed_at,
                )
                self._last_window_title = self._focus_current.title
            self._promote_deferred_focus(now=now)
            self._finalize_focus_settle(now=now)
            return

        # —— APP_FOCUS 切换 ——
        self._next_timer_at = 0.0
        self._invalidate_window_text_cache()
        self._last_text_hash = None
        self._focus_previous = current
        self._focus_current = snap
        self._last_window_title = snap.title
        self._ready_focus_trigger = ""

        if now - self._last_window_trigger_at >= self.config.window_switch_cooldown:
            self._arm_focus_settle(snap, now=now)
        else:
            self._defer_focus(snap)

        self._promote_deferred_focus(now=now)
        self._finalize_focus_settle(now=now)

    def _finalize_focus_settle(self, *, now: float) -> None:
        if self._focus_settled_at <= 0 or now < self._focus_settled_at:
            return
        pending = self._pending_focus
        current = self._focus_current
        if (
            pending is not None
            and current is not None
            and pending.app_key == current.app_key
        ):
            self._emit_ready_focus_trigger(
                previous=self._focus_previous,
                current=current,
                now=now,
            )
        self._pending_focus = None
        self._focus_settled_at = 0.0

    def _consume_focus_triggers(self, triggers: list[str]) -> None:
        if any(t.startswith("window:") for t in triggers):
            self._ready_focus_trigger = ""

    async def _collect_triggers(self) -> list[str]:
        now = time.monotonic()

        if self._away_mode:
            away_elapsed = now - self._away_set_at
            if away_elapsed >= self.config.away_max_seconds:
                self.set_away_mode(False)
                logger.info(
                    "ProactiveObserver: away_mode auto-expired after {:.0f}s",
                    away_elapsed,
                )
                _observer_gui_log("away_mode 超时自动恢复")
            else:
                return []

        if now - self._last_user_at < self.config.min_silence_after_user:
            return []

        # 焦点触发不走「刚说过话的全局冷却」——分层：开口冷却 ≠ 看屏冷却。
        # timer/content/idle 仍尊重 cooldown_seconds。
        speak_cooldown = bool(
            self._last_proactive_at
            and now - self._last_proactive_at < self.config.cooldown_seconds
        )
        eval_throttle = now - self._last_eval_at < self.config.poll_interval * 1.5

        triggers: list[str] = []
        if self._ready_focus_trigger:
            triggers.append(self._ready_focus_trigger)

        if eval_throttle and not triggers:
            return []
        if eval_throttle and triggers:
            # 允许带上已就绪的切窗触发，避免被短节流吞掉。
            return triggers

        if not speak_cooldown:
            if self._focus_settled_at == 0:
                timer_target = (
                    self._next_timer_at
                    if self._next_timer_at > 0
                    else (self._last_timer_check + self.config.timer_seconds)
                )
                if now >= timer_target:
                    triggers.append("timer")
                    self._last_timer_check = now
                    self._next_timer_at = 0.0

            if not triggers and self._focus_settled_at == 0 and not self._ready_focus_trigger:
                if now - self._last_content_check_at >= self.config.content_check_interval:
                    self._last_content_check_at = now
                    if self._check_content_changed():
                        if now - self._last_window_trigger_at >= self.config.window_switch_cooldown:
                            triggers.append("content")
                            self._last_window_trigger_at = now

            idle = get_idle_seconds()
            if idle >= self.config.idle_threshold_seconds and self._idle_armed:
                triggers.append(f"idle:{int(idle)}s")
                self._idle_armed = False

        return triggers

    def _invalidate_window_text_cache(self) -> None:
        self._cached_window_text = None
        self._cached_window_text_at = 0.0
        self._cached_window_title = ""

    def _store_window_text_cache(self, window_text: WindowText, title: str = "") -> None:
        self._cached_window_text = window_text
        self._cached_window_text_at = time.monotonic()
        self._cached_window_title = (
            title or window_text.window_title or get_active_window_title()
        )

    def _get_window_text_for_eval(self) -> WindowText:
        title = get_active_window_title()
        cached = self._cached_window_text
        if (
            cached is not None
            and title
            and title == self._cached_window_title
            and (time.monotonic() - self._cached_window_text_at)
            <= max(self.config.content_check_interval, 15.0)
        ):
            return cached
        window_text = _read_window_text_isolated()
        self._store_window_text_cache(window_text, title)
        return window_text

    def _check_content_changed(self) -> bool:
        blocked, matched = self.privacy.check_active_window()
        if blocked:
            logger.debug("ProactiveObserver: content check privacy skip ({})", matched)
            return False

        window_text = _read_window_text_isolated()
        title = get_active_window_title()
        self._store_window_text_cache(window_text, title)

        if not window_text.is_accessible:
            return False
        text = window_text.text_content.strip()
        if len(text) < self.config.content_min_chars:
            return False

        h = hash(text)
        if self._last_text_hash is None:
            self._last_text_hash = h
            return False
        if h != self._last_text_hash:
            self._last_text_hash = h
            return True
        return False

    def _looks_like_game_context(self, window_text: WindowText) -> bool:
        proc = (get_active_window_process_name() or window_text.process_name or "").casefold()
        if proc in _NON_GAME_PROCESSES:
            return False
        if any(hint in proc for hint in _GAME_PROCESS_HINTS):
            return True
        if window_text.app_type == "custom_ui":
            return True
        uia_chars = len(window_text.text_content.strip()) if window_text.is_accessible else 0
        if uia_chars < self.config.content_min_chars and proc and proc not in _NON_GAME_PROCESSES:
            return True
        return False

    def _safe_on_evaluate(self, reason: str, should_speak: bool) -> None:
        try:
            self.on_evaluate(reason, should_speak)
        except Exception as e:
            logger.warning("ProactiveObserver: on_evaluate callback error: {}", e)

    async def _do_evaluation(self, triggers: list[str]) -> None:
        now = time.monotonic()
        self._last_eval_at = now

        blocked, matched = self.privacy.check_active_window()
        if blocked:
            logger.info("ProactiveObserver: privacy block ({})", matched)
            _observer_gui_log("隐私拦截", {"matched": matched})
            self._safe_on_evaluate(f"隐私拦截：{matched}", False)
            return

        try:
            obs = self.capture.grab()
        except Exception as e:
            logger.warning("ProactiveObserver: screen capture failed: {}", e)
            _observer_gui_log("截图失败", {"error": str(e)})
            self._safe_on_evaluate(f"截图失败：{e}", False)
            return

        if obs.dhash and self._last_frame_dhash is not None:
            hamming = (obs.dhash ^ self._last_frame_dhash).bit_count()
            if hamming <= 4:
                logger.info(
                    "ProactiveObserver: frame dedup (hamming={}), skipping VLM",
                    hamming,
                )
                _observer_gui_log("画面重复，跳过 VLM 评估", {"hamming": hamming})
                self._last_frame_dhash = obs.dhash
                self._last_dedup_skip_at = now
                self._safe_on_evaluate("画面未变化（dHash去重）", False)
                return
        if obs.dhash:
            self._last_frame_dhash = obs.dhash

        window_text = self._get_window_text_for_eval()
        if window_text.is_accessible and window_text.text_content.strip():
            logger.debug(
                "ProactiveObserver: UIA read {} chars from {} elements in {:.0f}ms",
                len(window_text.text_content),
                window_text.element_count,
                window_text.walk_time_ms,
            )
            _observer_gui_log(
                "UIA 文字提取",
                {
                    "app_type": window_text.app_type,
                    "chars": len(window_text.text_content),
                    "elements": window_text.element_count,
                    "walk_ms": int(window_text.walk_time_ms),
                },
            )

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

        # 最近观测历史，避免对相似场景说重复的话
        obs_ctx = self._format_obs_history()
        if obs_ctx:
            ctx_parts.append(obs_ctx)

        # 最近对话历史，让 VLM 知道用户回复了什么
        try:
            chat_ctx = self._get_recent_history()
            if chat_ctx:
                ctx_parts.append(chat_ctx)
        except Exception:
            pass

        uia_enough = (
            window_text.is_accessible
            and len(window_text.text_content.strip()) >= self.config.content_min_chars
        )
        if window_text.is_accessible and window_text.text_content.strip():
            uia_lines = [f"[UIA 直接读取] 应用类型：{window_text.app_type}"]
            if window_text.process_name:
                uia_lines.append(f"进程：{window_text.process_name}")
            uia_lines.append(f"窗口内可见文字：\n{window_text.text_content}")
            ctx_parts.append("\n".join(uia_lines))

        # 游戏态 OCR 暂关（见 ProactiveConfig.game_ocr_enabled 默认 False）。
        # 重新启用：配置 game_ocr_enabled=true，或把下方条件改回仅看 config。
        if False and (
            self.config.game_ocr_enabled
            and not uia_enough
            and self._looks_like_game_context(window_text)
        ):
            _observer_gui_log("开始游戏态 OCR")
            ocr_text = await asyncio.to_thread(_ocr_game_dialogue_isolated)
            if ocr_text:
                proc = get_active_window_process_name()
                if proc:
                    ocr_block = (
                        f"[OCR 游戏文本] 进程：{proc}\n"
                        f"对话框区域识别（可能有误差）：\n{ocr_text}"
                    )
                else:
                    ocr_block = (
                        "[OCR 游戏文本] 对话框区域识别（可能有误差）：\n" + ocr_text
                    )
                ctx_parts.append(ocr_block)
                _observer_gui_log(
                    "游戏态 OCR",
                    {"chars": len(ocr_text), "process": proc or ""},
                )
            else:
                _observer_gui_log("游戏态 OCR 无结果（超时/失败/空）")

        ctx_text = "\n".join(ctx_parts) or "（无额外上下文）"
        user_text = f"{ctx_text}\n\n（截图见下）"

        messages = [
            {
                "role": "system",
                "content": [{"type": "text", "text": self._build_full_system_prompt()}],
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{obs.mime};base64,{obs.image_b64}"
                        },
                    },
                ],
            },
        ]

        _observer_gui_log(
            "正在调用视觉模型",
            {"model": self._api_model, "base_url": self._api_base_url},
        )
        try:
            response = await self._chat_completion(messages)
        except Exception as e:
            logger.warning(
                "ProactiveObserver: VLM call failed: {} ({})", e, type(e).__name__
            )
            _observer_gui_log(
                "VLM 调用失败", {"error": str(e), "type": type(e).__name__}
            )
            self._safe_on_evaluate(f"VLM 调用失败：{e}", False)
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
            logger.warning(
                "ProactiveObserver: no JSON in response: {!r}", response[:200]
            )
            _observer_gui_log(
                "VLM 返回无法解析",
                {"preview": (response or "")[:120], "model": self._api_model},
            )
            self._safe_on_evaluate(
                "VLM 返回无法解析为 JSON（请确认 vision 槽位用支持识图的模型）",
                False,
            )
            return

        suggested = parsed.get("suggested_interval")
        if isinstance(suggested, (int, float)) and suggested > 0:
            clamped = max(
                self.config.adaptive_interval_min,
                min(float(suggested), self.config.adaptive_interval_max),
            )
            self._next_timer_at = time.monotonic() + clamped
            logger.debug(
                "ProactiveObserver: adaptive interval set to {:.0f}s (requested {:.0f}s)",
                clamped,
                suggested,
            )
        else:
            self._next_timer_at = 0.0

        if not parsed.get("should_speak"):
            reason = str(parsed.get("reason", "")).strip() or "模型选择不发言"
            logger.info("ProactiveObserver: silent (reason: {})", reason)
            self._safe_on_evaluate(reason, False)
            self._record_observation(window_title, False, reason)
            return

        comment = str(parsed.get("comment", "")).strip()
        if not comment:
            reason = (
                str(parsed.get("reason", "")).strip()
                or "should_speak=true 但 comment 为空"
            )
            logger.warning("ProactiveObserver: {}", reason)
            self._safe_on_evaluate(reason, False)
            self._record_observation(window_title, False, reason)
            return

        reason = str(parsed.get("reason", "")).strip() or "主动搭话"
        self._safe_on_evaluate(reason, True)

        self._last_proactive_at = time.monotonic()
        self._idle_armed = True

        self._record_observation(window_title, True, reason, comment)

        payload = ProactiveSpeakPayload(
            text=comment,
            translation=str(parsed.get("translation", "")).strip(),
            tone=str(parsed.get("tone", "")).strip() or "中性",
        )

        try:
            self.on_speak(payload)
        except Exception as e:
            logger.warning("ProactiveObserver: on_speak callback error: {}", e)
            _observer_gui_log("主动发言回调失败", {"error": str(e)})

    def _record_observation(
        self,
        window_title: str,
        should_speak: bool,
        reason: str,
        comment: str = "",
    ) -> None:
        self._obs_history.append(
            ObservationRecord(
                timestamp=time.monotonic(),
                window_title=window_title,
                should_speak=should_speak,
                reason=reason,
                comment=comment,
            )
        )

    def _format_obs_history(self) -> str:
        if not self._obs_history:
            return ""
        now = time.monotonic()
        lines = ["[最近の観測履歴]"]
        for r in reversed(self._obs_history):
            ago_s = int(now - r.timestamp)
            if ago_s < 60:
                ago_str = f"{ago_s}秒前"
            elif ago_s < 3600:
                ago_str = f"{ago_s // 60}分前"
            else:
                ago_str = f"{ago_s // 3600}時間前"
            win = r.window_title or "(未知窗口)"
            if r.should_speak:
                line = (
                    f"- {ago_str} | {win} | 发言：\u300c{r.comment}\u300d | {r.reason}"
                )
            else:
                line = f"- {ago_str} | {win} | 不说话 | {r.reason}"
            lines.append(line)
        return "\n".join(lines)

    def _build_full_system_prompt(self) -> str:
        parts = []
        if self._system_prompt.strip():
            parts.append(self._system_prompt.strip())
        parts.append(_PROACTIVE_SYSTEM_PROMPT)
        return "\n\n---\n\n".join(parts)

    async def _chat_completion(self, messages: list[dict]) -> str:
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
                finish,
                self._api_model,
            )
            logger.debug(
                "ProactiveObserver: raw response: {}",
                json.dumps(data, ensure_ascii=False)[:500],
            )
        return content or ""


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None
