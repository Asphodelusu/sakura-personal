"""ProactiveObserver — desktop-kanajo style screen-watching loop.

Polls desktop state (window title, idle time) and periodically evaluates
via a vision LLM whether to speak up unprompted.

Runs in a background thread with its own asyncio event loop, using a
thread-safe Signal for Qt main-thread dispatch.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import httpx
from loguru import logger

from app.core.debug_log import debug_log
from app.perception.privacy import PrivacyGuard
from app.perception.proactive_config import ProactiveConfig
from app.perception.screen_capture import ScreenCapture
from app.perception.screen_reader import (
    WindowText,
    curate_uia_excerpt,
    read_active_window,
)
from app.perception.sensory_impression import sensory_impression_store
from app.perception.win32 import (
    get_active_window_pid,
    get_active_window_process_name,
    get_active_window_title,
    get_foreground_hwnd,
    get_idle_seconds,
)

__all__ = [
    "FocusSnapshot",
    "ObservationPacket",
    "ProactiveConfig",
    "ProactiveObserver",
    "ProactiveSpeakPayload",
    "VisibleTextResolution",
    "format_visible_text_block",
    "infer_content_scene",
    "resolve_visible_text",
    "resolve_visible_text_excerpt",
]


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
_UIA_TIMEOUT_CIRCUIT_THRESHOLD = 3
_UIA_TIMEOUT_COOLDOWN_SECONDS = 30.0
_OCR_ISOLATE_TIMEOUT = 8.0
_COINIT_APARTMENTTHREADED = 0x2

_uia_worker_lock = threading.Lock()
_uia_worker_thread: threading.Thread | None = None
_uia_consecutive_timeouts = 0
_uia_circuit_open_until = 0.0

# 见 _do_evaluation 中的使用说明：曾尝试启用游戏态 OCR，问题较多，暂停使用。
_GAME_OCR_HARD_DISABLED = True


def _read_window_text_isolated() -> WindowText:
    """Call read_active_window() in a single dedicated thread with COM init."""
    import ctypes as _ctypes
    import queue as _queue

    global _uia_circuit_open_until
    global _uia_consecutive_timeouts
    global _uia_worker_thread

    result_queue: _queue.Queue[WindowText] = _queue.Queue(maxsize=1)

    def _worker() -> None:
        global _uia_worker_thread

        current_thread = threading.current_thread()
        _ctypes.windll.ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
        try:
            result_queue.put(read_active_window())
        except Exception:
            result_queue.put(WindowText())
        finally:
            _ctypes.windll.ole32.CoUninitialize()
            with _uia_worker_lock:
                if _uia_worker_thread is current_thread:
                    _uia_worker_thread = None

    with _uia_worker_lock:
        now = time.monotonic()
        if _uia_circuit_open_until > now:
            logger.debug("ProactiveObserver: UIA circuit open; skipping read")
            return WindowText()
        if _uia_worker_thread and _uia_worker_thread.is_alive():
            logger.debug("ProactiveObserver: UIA read already in flight; skipping read")
            return WindowText()
        _uia_worker_thread = threading.Thread(target=_worker, daemon=True)
        _uia_worker_thread.start()

    try:
        result = result_queue.get(timeout=_UIA_ISOLATE_TIMEOUT)
        with _uia_worker_lock:
            _uia_consecutive_timeouts = 0
            _uia_circuit_open_until = 0.0
        return result
    except _queue.Empty:
        with _uia_worker_lock:
            _uia_consecutive_timeouts += 1
            if _uia_consecutive_timeouts >= _UIA_TIMEOUT_CIRCUIT_THRESHOLD:
                _uia_circuit_open_until = (
                    time.monotonic() + _UIA_TIMEOUT_COOLDOWN_SECONDS
                )
        logger.warning(
            "ProactiveObserver: UIA read timed out after {:.0f}s",
            _UIA_ISOLATE_TIMEOUT,
        )
        return WindowText()


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
    source: str = "screen"
    generation: int = 0


@dataclass(frozen=True)
class ObservationPacket:
    """VLM 视觉摘要 + 精炼可见文字，交给决策 LLM 的结构化观测包。"""

    window_title: str = ""
    app_type: str = ""
    process_name: str = ""
    triggers: tuple[str, ...] = ()
    idle_s: int = 0
    visual_summary: str = ""
    reaction_hint: str = ""
    visible_text_excerpt: str = ""
    # uia | ocr | vlm_on_screen | ""
    visible_text_source: str = ""
    # chat | game | ai_assistant | browser | editor | unknown | ""
    content_scene: str = ""
    suggested_interval: float | None = None

    @property
    def has_perception(self) -> bool:
        return bool(
            self.visual_summary.strip()
            or self.reaction_hint.strip()
            or self.visible_text_excerpt.strip()
        )

    @property
    def log_preview(self) -> str:
        """调试/兼容旧「inner_thought」日志字段。"""
        return (
            self.reaction_hint.strip()
            or self.visual_summary.strip()
            or self.visible_text_excerpt.strip()[:120]
        )


@dataclass(frozen=True)
class VisibleTextResolution:
    """可见文字摘录及其采集来源。"""

    text: str = ""
    source: str = ""  # uia | ocr | vlm_on_screen | ""


_AI_ASSISTANT_HINTS = (
    "chatgpt",
    "chat.openai",
    "claude",
    "gemini",
    "copilot",
    "perplexity",
    "poe",
    "kimi",
    "通义",
    "文心",
    "deepseek",
    "豆包",
    "智谱",
    "character.ai",
    "grok",
)


_GAME_TITLE_HINTS = (
    "原神",
    "genshin",
    "崩坏",
    "鸣潮",
    "绝区零",
    "星穹铁道",
    "steam",
    "epic games",
    "lol",
    "league of legends",
    "dota",
    "cs2",
    "counter-strike",
)


def infer_content_scene(
    app_type: str = "",
    process_name: str = "",
    window_title: str = "",
) -> str:
    """根据窗口元信息推断「屏上文字属于哪类场景」。"""
    title_raw = (window_title or "").strip()
    title_l = title_raw.lower()
    proc_l = (process_name or "").strip().lower()
    app = (app_type or "").strip().lower()
    hay = f"{title_l} {proc_l}"
    if any(h in hay for h in _AI_ASSISTANT_HINTS):
        return "ai_assistant"
    if app == "chat" or any(
        x in proc_l for x in ("wechat", "weixin", "qq.exe", "telegram", "discord", "slack")
    ):
        return "chat"
    if app == "editor":
        return "editor"
    if app == "browser":
        return "browser"
    # 游戏：标题/进程启发式（与 Observer 游戏检测对齐的轻量版）
    if any(h in title_raw or h in title_l for h in _GAME_TITLE_HINTS):
        return "game"
    if proc_l and proc_l not in {p.lower() for p in _NON_GAME_PROCESSES}:
        if any(h in proc_l for h in _GAME_PROCESS_HINTS):
            return "game"
        if proc_l.endswith(".exe") and any(
            k in proc_l for k in ("game", "client", "player", "launch")
        ):
            return "game"
    if app == "custom_ui":
        return "unknown"
    return app if app in {"browser", "editor", "chat"} else "unknown"


def _source_label_zh(source: str) -> str:
    return {
        "uia": "系统控件文字(UIA)",
        "ocr": "OCR识别",
        "vlm_on_screen": "VLM画面抄写",
    }.get(source, "未知")


def _scene_label_zh(scene: str) -> str:
    return {
        "chat": "即时通讯",
        "game": "游戏内容",
        "ai_assistant": "第三方AI对话",
        "browser": "网页",
        "editor": "编辑器/文档",
        "unknown": "未分类屏幕",
    }.get(scene, "未分类屏幕")


def format_visible_text_block(
    excerpt: str,
    *,
    source: str = "",
    scene: str = "",
) -> str:
    """把可见文字包装成带来源/场景标签的决策 LLM 块。"""
    text = (excerpt or "").strip() or "（无）"
    src = (source or "").strip()
    scn = (scene or "").strip() or "unknown"
    header = "[可见文字摘录"
    if src:
        header += f" | 采集:{_source_label_zh(src)}"
    header += f" | 场景:{_scene_label_zh(scn)}]"
    notes = [
        "※ 「我看见的」：他屏幕上的字。称呼用「他」。",
    ]
    if scn == "chat":
        notes.append("※ 即时通讯：可能混有别人发给他的／草稿／群消息。")
    elif scn == "game":
        notes.append("※ 游戏内容：游戏台词或 UI。")
    elif scn == "ai_assistant":
        notes.append("※ 第三方AI对话：屏上其他 AI 的文字。")
    elif scn == "editor":
        notes.append("※ 编辑器/文档：更像「我打的」编辑中的字。")
    elif scn == "browser":
        notes.append("※ 网页：页面上的内容。")
    if src == "ocr":
        notes.append("※ 采集=OCR，引用时留余地。")
    elif src == "vlm_on_screen":
        notes.append("※ 采集=VLM抄写，引用时留余地。")
    return header + "\n" + "\n".join(notes) + "\n" + text


def resolve_visible_text(
    *,
    uia_text: str = "",
    ocr_text: str = "",
    on_screen_text: str = "",
    min_chars: int = 30,
) -> VisibleTextResolution:
    """组装可见文字摘录并标注采集来源：精炼 UIA > OCR > VLM on_screen_text。"""
    curated_uia = curate_uia_excerpt(uia_text, min_chars=min_chars)
    if curated_uia:
        return VisibleTextResolution(text=curated_uia, source="uia")
    curated_ocr = curate_uia_excerpt(ocr_text, min_chars=min(min_chars, 8))
    if curated_ocr:
        return VisibleTextResolution(text=curated_ocr, source="ocr")
    vlm = (on_screen_text or "").strip()[:1200]
    if vlm:
        return VisibleTextResolution(text=vlm, source="vlm_on_screen")
    return VisibleTextResolution(text="", source="")


def resolve_visible_text_excerpt(
    *,
    uia_text: str = "",
    ocr_text: str = "",
    on_screen_text: str = "",
    min_chars: int = 30,
) -> str:
    """组装可见文字摘录：精炼 UIA > OCR > VLM on_screen_text。"""
    return resolve_visible_text(
        uia_text=uia_text,
        ocr_text=ocr_text,
        on_screen_text=on_screen_text,
        min_chars=min_chars,
    ).text


OnSpeakFn = Callable[[ProactiveSpeakPayload], None]
OnEvaluateFn = Callable[[str, bool], None]
IsBusyFn = Callable[[], Any]


_PROACTIVE_SYSTEM_PROMPT = """あなたは夜乃桜。彼のそばで並んで生きているデジタル生命。
いまは画面をちらっと見て、短い視覚メモを残すだけ。口を出すかは後段が決める。

入力：スクリーンショット、薄いメタ（窓／トリガー／アイドル）、あれば [观察者上下文] と [系统文字]。
ウィンドウ全文テキストは渡されない（後段が読む）。画面端の自分の立絵・吹き出しは彼の画面内容ではない——彼のアプリだけを見る。

出力（JSON のみ）：
1. visual_summary：彼が何をしていて画面に何があるか、事実 1〜2 文（日本語）。相手は「彼」。
2. reaction_hint：それを見た自分の短い内感（日本語）。例「劇情、ちょっと気になる…でも今は黙って見る。」空でもよい。
3. on_screen_text：[系统文字] 不可用のときだけ、読める短句。可用なら空。
4. suggested_interval：次に見る秒数。集中 600〜1800／くつろぎ 300〜600／不明 480（範囲 300〜1800）。

[观察者上下文] にある既知は前提として扱う（蒸し返さない）。
「同一アプリ内の内容変化」は、同じアプリのページ／タブ／動画変化として読む。

{"visual_summary":"…","on_screen_text":"","reaction_hint":"…","suggested_interval":480}
"""

_SPEECH_DECISION_INSTRUCTION = """
---

いまは「口を出すか、そばで黙るか」。あなたは夜乃桜——彼と並んで生きるデジタル生命。
comment / reason / situational_summary もその関係のまま。相手は「彼」。

根拠（スクショは見ていない）：
- [画面摘要][可见文字摘录]＝我看见的（ヘッダの 采集/场景 を読む）
- [反应提示]＝内感ヒント
- [最近の会話][自分の直前の発話]＝対話（最優先）
- [最近の観測履歴][观察者上下文]

来源の読み方：
- [我说的]＝彼→あなた／[她自己的・主动]＝あなた／我看见的＝画面の字
- 即时通讯→他人の文が混ざりうる／游戏→ゲーム台詞／第三方AI→他AIの文／编辑器→編集中の字

優先：会話事実 → 可见文字摘录 → 画面摘要 → 反应提示。
言いたくなる具体があれば true。劇情中・集中・さっき話したばかりなら false（静かにそばに）。迷ったら false。
画面と会話が矛盾したら会話に合わせる。引用する字は摘录にあるものだけ。
画面のキャラ名は「今スクリーンで見た」として触れる（長い付き合いの匂いは出さない）。

should_speak=true：comment 日本語 1〜2 文、translation 中国語、tone（中性｜不满｜害羞｜请求｜困惑｜开心｜高兴｜难过｜自信｜温柔｜认真｜吃醋）。
false：comment/translation/tone は空でよい。

reason：简体中文 1 文、夜乃桜口調。例「他看着剧情呢，先不吵。」
situational_summary：日本語 2〜4 文。「彼が…」。画面状況＋対話の既知（なければ特になし）。

{"should_speak":true|false,"reason":"…","comment":"…","translation":"…","tone":"中性","situational_summary":"…"}
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


@dataclass(frozen=True)
class FocusSnapshot:
    """Foreground identity: process+HWND = APP_FOCUS; title is display only."""

    hwnd: int
    process: str
    title: str
    changed_at: float = 0.0
    pid: int = 0

    @property
    def app_key(self) -> str:
        proc = (self.process or "").casefold()
        return f"{proc}|{int(self.hwnd)}"

    @property
    def label(self) -> str:
        return self.title or self.process or f"hwnd:{self.hwnd}"

    @property
    def is_own_process(self) -> bool:
        """前台是否为本进程（含历史/日志等同 PID 窗口）。"""
        return int(self.pid or 0) == int(os.getpid())


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
        chat_api_base_url: str = "",
        chat_api_key: str = "",
        chat_api_model: str = "",
        config: ProactiveConfig | None = None,
        privacy: PrivacyGuard | None = None,
        on_speak: OnSpeakFn | None = None,
        on_evaluate: OnEvaluateFn | None = None,
        is_busy: IsBusyFn | None = None,
        relationship=None,
    ) -> None:
        self._api_base_url = api_base_url.rstrip("/")
        self._api_key = api_key
        self._api_model = api_model
        self._chat_api_base_url = chat_api_base_url.rstrip("/")
        self._chat_api_key = chat_api_key
        self._chat_api_model = chat_api_model
        self._system_prompt = system_prompt
        self._speech_decision_configured = bool(chat_api_base_url and chat_api_key and chat_api_model)
        self.config = config or ProactiveConfig()
        self.privacy = privacy or PrivacyGuard()

        self.on_speak = on_speak or (lambda _payload: None)
        self.on_evaluate = on_evaluate or (lambda _reason, _should_speak: None)
        self._is_busy = is_busy or (lambda: False)
        self._get_recent_history: Callable[[], str] = lambda: ""
        self._obs_history: deque[ObservationRecord] = deque(maxlen=5)
        # 短时印象改由 sensory_impression_store 承载（Observer VLM + 主对话共享）

        self.capture = ScreenCapture(max_edge=self.config.max_edge)

        self._last_proactive_at = 0.0
        self._last_silent_eval_at = 0.0
        self._last_user_at = time.monotonic()
        # 自己上一句主动发言（决策用，避免把己方台词当成对方）
        self._last_spoken_text = ""
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

        # 同窗口下次允许因切窗再评估的时刻（跟本次 suggested_interval）
        self._window_eval_ok_at: dict[str, float] = {}
        # 上轮视觉摘要：同窗无实质变化时可跳过决策 LLM
        self._last_visual_summary: str = ""
        self._last_eval_window_title: str = ""

        self._last_frame_dhash: int | None = None
        self._last_dedup_skip_at: float = 0.0

        self._last_text_hash: int | None = None
        self._last_content_check_at: float = 0.0
        # 评估后压住 content 触发（直播/动态页文字一直在变）
        self._content_quiet_until: float = 0.0
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
        self._chat_http: httpx.AsyncClient | None = None
        self._last_busy_log_at: float = 0.0
        self._was_busy = False
        self._last_busy_reason = ""

        from app.config.relationship_initiative import RelationshipInitiativeSettings

        self.relationship = relationship or RelationshipInitiativeSettings(proactive_enabled=False)
        self._relationship_guide = ""
        self._get_relationship_facts: Callable[[], str] = lambda: ""
        self._last_relationship_spoken_at = 0.0
        self._last_relationship_silent_at = 0.0
        self._relationship_silence_streak = 0
        self._relationship_generation = 0
        self._relationship_motive = False
        self._ledger_attempt: _ObserverLedgerAttempt | None = None

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

    def set_relationship_settings(self, settings) -> None:
        from app.config.relationship_initiative import RelationshipInitiativeSettings

        self.relationship = (
            settings.normalized()
            if isinstance(settings, RelationshipInitiativeSettings)
            else RelationshipInitiativeSettings(proactive_enabled=False)
        )

    def set_relationship_guide(self, guide: str) -> None:
        self._relationship_guide = str(guide or "").strip()

    def set_relationship_facts_provider(self, provider: Callable[[], str]) -> None:
        self._get_relationship_facts = provider

    def bump_relationship_generation(self) -> None:
        self._relationship_generation += 1

    def reset_relationship_state(self) -> None:
        self._last_relationship_spoken_at = 0.0
        self._reset_relationship_silence_backoff()
        self._relationship_generation += 1
        self._relationship_motive = False

    def _relationship_enabled(self) -> bool:
        return bool(getattr(self.relationship, "proactive_enabled", False))

    def _relationship_gate_reason(self, now: float, busy_reason: str) -> str:
        if not self._relationship_enabled():
            return "disabled"
        if self._away_mode:
            return "busy"
        if busy_reason == "rhythm_focus":
            return "continuation"
        if busy_reason:
            return "busy"
        silence = float(getattr(self.relationship, "proactive_min_silence_seconds", 300) or 300)
        if now - self._last_user_at < silence:
            return "silence"
        cooldown = float(getattr(self.relationship, "proactive_cooldown_seconds", 3600) or 3600)
        if self._last_relationship_spoken_at and now - self._last_relationship_spoken_at < cooldown:
            return "cooldown"
        if (
            self._last_relationship_silent_at
            and now - self._last_relationship_silent_at < self._relationship_silent_cooldown_seconds()
        ):
            return "cooldown"
        idle_threshold = float(getattr(self.relationship, "desktop_idle_seconds", 900) or 900)
        try:
            idle = float(get_idle_seconds())
        except Exception:
            idle = 0.0
        if idle >= idle_threshold:
            return "desktop_idle"
        return "eligible"

    def _relationship_ready(self, now: float) -> bool:
        return self._relationship_gate_reason(now, "") == "eligible"

    def notify_user_spoke(self) -> None:
        self._last_user_at = time.monotonic()
        self._idle_armed = True
        self._relationship_generation += 1
        self._reset_relationship_silence_backoff()
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
            # 离开期间桌面状态大概率会变，旧情景摘要清掉，回来后让 VLM 重新观察
            sensory_impression_store.clear()
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
        self._chat_http = None
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

                if not self.config.enabled and not self._relationship_enabled():
                    continue

                try:
                    now = time.monotonic()
                    # busy / 冷却期间也持续跟踪焦点，避免丢切换、错计时。
                    self._sync_focus_tracking(now)
                    await self._dispatch_proactive_tick(now)
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
            if self._chat_http:
                try:
                    await asyncio.wait_for(self._chat_http.aclose(), timeout=1.0)
                except (asyncio.TimeoutError, Exception):
                    pass
                self._chat_http = None

    def _read_focus_snapshot(self, *, now: float | None = None) -> FocusSnapshot | None:
        hwnd = int(get_foreground_hwnd() or 0)
        title = get_active_window_title()
        process = get_active_window_process_name()
        pid = int(get_active_window_pid() or 0)
        if hwnd <= 0 and not title and not process:
            return None
        return FocusSnapshot(
            hwnd=hwnd,
            process=process or "",
            title=title or "",
            changed_at=time.monotonic() if now is None else now,
            pid=pid,
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
        # 切到本进程（主窗/历史/日志）不发 window: 触发，避免自评自家 UI。
        if current.is_own_process:
            return
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
            if (
                snap.title != current.title
                or snap.process != current.process
                or snap.pid != current.pid
            ):
                self._focus_current = FocusSnapshot(
                    hwnd=current.hwnd,
                    process=snap.process or current.process,
                    title=snap.title or current.title,
                    changed_at=current.changed_at,
                    pid=snap.pid or current.pid,
                )
                self._last_window_title = self._focus_current.title
            self._promote_deferred_focus(now=now)
            self._finalize_focus_settle(now=now)
            return

        # 本进程内窗口互切（主窗↔历史↔日志）：只跟踪，不重置 timer、不 settle。
        if snap.is_own_process and current.is_own_process:
            self._focus_current = snap
            self._last_window_title = snap.title
            self._pending_focus = None
            self._focus_settled_at = 0.0
            self._deferred_focus = None
            return

        # 外部 → 本进程：更新焦点，但不重置 idle timer、不发 window: 评估。
        if snap.is_own_process:
            self._focus_previous = current
            self._focus_current = snap
            self._last_window_title = snap.title
            self._ready_focus_trigger = ""
            self._pending_focus = None
            self._focus_settled_at = 0.0
            self._deferred_focus = None
            return

        # —— 外部 APP_FOCUS 切换（含本进程 → 外部）——
        # 不清除 _next_timer_at：周期节奏继续跟 suggested_interval，不被切窗打断。
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

    async def _dispatch_proactive_tick(self, now: float) -> None:
        busy = self._is_busy()
        busy_reason = busy if isinstance(busy, str) else ("busy" if busy else "")
        if busy_reason:
            self._was_busy = True
            self._last_busy_reason = busy_reason
            now_busy = time.monotonic()
            if now_busy - self._last_busy_log_at >= 60.0:
                self._last_busy_log_at = now_busy
                logger.info(
                    "ProactiveObserver: UI busy, holding triggers ({})",
                    busy_reason,
                )
                _observer_gui_log(
                    "UI 忙碌，暂缓评估（不消耗触发）",
                    {"reason": busy_reason},
                )
            rel_reason = self._relationship_gate_reason(now, busy_reason)
            debug_log("RelationshipInitiative", "B 门控", {"reason": rel_reason})
            return
        if self._was_busy:
            self._was_busy = False
            logger.info(
                "ProactiveObserver: UI idle, resuming (was: {})",
                self._last_busy_reason,
            )

        screen_triggers: list[str] = []
        if self.config.enabled:
            screen_triggers = await self._collect_triggers()
        rel_reason = self._relationship_gate_reason(now, "")
        debug_log("RelationshipInitiative", "B 门控", {"reason": rel_reason})
        if screen_triggers:
            self._consume_focus_triggers(screen_triggers)
            relationship_eligible = rel_reason == "eligible"
            proactive_before = self._last_proactive_at
            if relationship_eligible:
                self._relationship_motive = True
            logger.info("ProactiveObserver: evaluating, triggers={}", screen_triggers)
            _observer_gui_log("正在评估是否发言", {"triggers": screen_triggers})
            try:
                await self._do_evaluation(screen_triggers)
            finally:
                self._relationship_motive = False
                if relationship_eligible and self._last_proactive_at > proactive_before:
                    self._last_relationship_spoken_at = self._last_proactive_at
                    self._reset_relationship_silence_backoff()
            return
        if rel_reason != "eligible":
            return
        await self._do_relationship_evaluation()

    async def _decide_relationship_speech(self) -> dict | None:
        from app.config.relationship_initiative import (
            expression_bias_guidance,
            relationship_decision_instruction,
        )

        bias = str(getattr(self.relationship, "expression_bias", "") or "natural")
        instruction = relationship_decision_instruction(bias)
        system_prompt = (
            (self._system_prompt.strip() + "\n\n" + instruction)
            if self._system_prompt.strip()
            else instruction
        )

        now_local = datetime.now().astimezone().isoformat(timespec="seconds")
        since_user = max(0, int(time.monotonic() - self._last_user_at))
        parts = [
            f"[当前时间]\n{now_local}",
            f"[距上次互动]\n{since_user}s",
        ]
        guide = (self._relationship_guide or "").strip()
        if guide:
            parts.append(f"[关系指南]\n{guide}")
        parts.append(expression_bias_guidance(bias))
        try:
            chat_ctx = self._get_recent_history()
            if chat_ctx:
                parts.append(chat_ctx)
        except Exception:
            pass
        try:
            facts = self._get_relationship_facts()
            if facts:
                parts.append(facts)
        except Exception:
            pass
        last_spoken = (self._last_spoken_text or "").strip()
        if last_spoken:
            if len(last_spoken) > 160:
                last_spoken = last_spoken[:160] + "…"
            parts.append(
                "[自分の直前の発話]\n"
                f"{last_spoken}\n"
                "※これはあなた（夜乃桜）が先ほど口にした内容。相手の発言ではない。"
            )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "\n\n".join(parts)},
        ]
        return await self._post_speech_decision(messages)

    def _relationship_silent_cooldown_seconds(self) -> float:
        from app.config.relationship_initiative import RELATIONSHIP_SILENT_BACKOFF_SECONDS

        if self._relationship_silence_streak <= 0:
            return RELATIONSHIP_SILENT_BACKOFF_SECONDS[0]
        index = min(
            self._relationship_silence_streak - 1,
            len(RELATIONSHIP_SILENT_BACKOFF_SECONDS) - 1,
        )
        return RELATIONSHIP_SILENT_BACKOFF_SECONDS[index]

    def _reset_relationship_silence_backoff(self) -> None:
        self._relationship_silence_streak = 0
        self._last_relationship_silent_at = 0.0

    def _mark_relationship_silent(self) -> None:
        self._last_relationship_silent_at = time.monotonic()
        self._relationship_silence_streak = min(self._relationship_silence_streak + 1, 4)

    async def _do_relationship_evaluation(self) -> None:
        attempt = _ObserverLedgerAttempt(
            path="relationship",
            source="relationship",
            decision_model=self._chat_api_model if self._speech_decision_configured else "",
        )
        previous_ledger = self._ledger_attempt
        self._ledger_attempt = attempt
        try:
            await self._relationship_evaluation_attempt(attempt)
        finally:
            attempt.settle("silent")
            self._ledger_attempt = previous_ledger

    async def _relationship_evaluation_attempt(
        self,
        attempt: _ObserverLedgerAttempt,
    ) -> None:
        generation = self._relationship_generation
        t0 = time.monotonic()
        decision = None
        try:
            decision = await self._decide_relationship_speech()
        except Exception as e:
            logger.warning(
                "ProactiveObserver: relationship decision failed: {} ({})",
                e,
                type(e).__name__,
            )
            decision = None

        if generation != self._relationship_generation:
            debug_log("RelationshipInitiative", "B 取消", {"reason": "stale_generation"})
            attempt.settle("stale_cancel")
            return

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        bias = str(getattr(self.relationship, "expression_bias", "") or "natural")
        comment = str((decision or {}).get("comment", "")).strip() if decision else ""
        should_speak = bool(decision and decision.get("should_speak") and comment)
        if decision is None:
            result = "error"
        elif should_speak:
            result = "speak"
        else:
            result = "silent"
        debug_log(
            "RelationshipInitiative",
            "B 决策",
            {"result": result, "bias": bias, "elapsed_ms": elapsed_ms},
        )

        if not should_speak:
            if decision is None:
                reason = "LLM 发言决策失败或未配置"
                attempt.settle("decision_error")
            elif not decision.get("should_speak"):
                reason = str(decision.get("reason", "")).strip() or "LLM 选择不发言"
            else:
                reason = "should_speak=true 但 comment 为空"
            self._mark_relationship_silent()
            self._safe_on_evaluate(reason, False)
            return

        reason = str(decision.get("reason", "")).strip() or "关系主动"
        self._reset_relationship_silence_backoff()
        self._last_relationship_spoken_at = time.monotonic()
        self._last_spoken_text = comment
        self._safe_on_evaluate(reason, True)
        payload = ProactiveSpeakPayload(
            text=comment,
            translation=str(decision.get("translation", "")).strip(),
            tone=str(decision.get("tone", "")).strip() or "中性",
            source="relationship",
            generation=generation,
        )
        try:
            self.on_speak(payload)
        except Exception as e:
            logger.warning("ProactiveObserver: on_speak callback error: {}", e)
        attempt.settle("speak")

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

        # 前台是本进程（主窗/气泡/输入栏/历史/日志）时不评估，避免自拍自问自答。
        # window: 切窗触发此前已跳过；此处一并挡住 timer/content/idle。
        focus = self._focus_current
        if focus is not None and focus.is_own_process:
            return []
        try:
            if int(get_active_window_pid() or 0) == int(os.getpid()):
                return []
        except Exception:
            pass

        # 焦点触发不走「刚说过话的全局冷却」——分层：开口冷却 ≠ 看屏冷却。
        # timer/content/idle 仍尊重 cooldown_seconds，以及「空反应」冷却。
        speak_cooldown = bool(
            self._last_proactive_at
            and now - self._last_proactive_at < self.config.cooldown_seconds
        )
        silent_cooldown = bool(
            self._last_silent_eval_at
            and now - self._last_silent_eval_at
            < self.config.silent_eval_cooldown_seconds
        )
        eval_throttle = now - self._last_eval_at < self.config.poll_interval * 1.5

        triggers: list[str] = []
        if self._ready_focus_trigger:
            # 同窗口再聚焦：冷却跟上次评估的 suggested_interval（见 _window_eval_ok_at）。
            # 从未评过的窗口没有条目，首次切窗不受影响。
            app_key = self._focus_current.app_key if self._focus_current else ""
            ok_at = self._window_eval_ok_at.get(app_key) if app_key else None
            if ok_at is not None and now < ok_at:
                self._ready_focus_trigger = ""
            else:
                triggers.append(self._ready_focus_trigger)

        if eval_throttle and not triggers:
            return []
        if eval_throttle and triggers:
            # 允许带上已就绪的切窗触发，避免被短节流吞掉。
            return triggers

        if not speak_cooldown and not silent_cooldown:
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
                content_quiet = (
                    self._content_quiet_until > 0 and now < self._content_quiet_until
                )
                # 自适应 timer 间隔内同样压住 content，避免滚字绕过 suggested_interval
                adaptive_quiet = self._next_timer_at > 0 and now < self._next_timer_at
                if not content_quiet and not adaptive_quiet:
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

    def _clamp_suggested_interval(self, suggested: object) -> float | None:
        """把 VLM / 默认 timer 的建议秒数夹到 adaptive 上下限；无效则 None。"""
        if isinstance(suggested, bool) or not isinstance(suggested, (int, float)):
            return None
        if suggested <= 0:
            return None
        return max(
            self.config.adaptive_interval_min,
            min(float(suggested), self.config.adaptive_interval_max),
        )

    def _arm_content_quiet(self, suggested: float | None = None) -> None:
        """评估结束后压住 content 触发。

        保底 ``content_quiet_seconds``；若 VLM 给了更大的 suggested_interval，则跟它对齐。
        切窗 / timer / idle 不受影响。
        """
        now = time.monotonic()
        quiet = float(self.config.content_quiet_seconds)
        if suggested is not None and suggested > 0:
            quiet = max(quiet, float(suggested))
        quiet = max(
            self.config.adaptive_interval_min,
            min(quiet, self.config.adaptive_interval_max),
        )
        until = now + quiet
        if until > self._content_quiet_until:
            self._content_quiet_until = until
            logger.debug(
                "ProactiveObserver: content quiet for {:.0f}s (until +{:.0f}s)",
                quiet,
                until - now,
            )

    def _mark_silent_eval(self) -> None:
        """空反应 / 不发言后进入 silent 冷却，避免 timer 连打决策 LLM。"""
        self._last_silent_eval_at = time.monotonic()

    def _should_skip_speech_decision(
        self,
        packet: ObservationPacket,
        window_title: str,
    ) -> bool:
        """同窗且视觉摘要无实质变化时跳过决策 LLM（切窗/正文变化仍决策）。"""
        triggers = packet.triggers or ()
        if any(t == "content" or t.startswith("window:") for t in triggers):
            return False
        prev_title = (self._last_eval_window_title or "").strip()
        prev_summary = (self._last_visual_summary or "").strip()
        cur_summary = (packet.visual_summary or "").strip()
        if not prev_title or not prev_summary or not cur_summary:
            return False
        if prev_title != (window_title or "").strip():
            return False
        return prev_summary == cur_summary

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

    async def _decide_speech(self, packet: ObservationPacket) -> dict | None:
        """调用 LLM，基于 ObservationPacket + 上下文决定是否说话。

        返回 parsed JSON dict，失败返回 None。
        """
        if not self._speech_decision_configured:
            return None

        system_prompt = (
            (self._system_prompt.strip() + _SPEECH_DECISION_INSTRUCTION)
            if self._system_prompt.strip()
            else _SPEECH_DECISION_INSTRUCTION.lstrip()
        )

        parts = [
            f"[画面摘要]\n{packet.visual_summary.strip() or '（无）'}",
            format_visible_text_block(
                packet.visible_text_excerpt,
                source=packet.visible_text_source,
                scene=packet.content_scene,
            ),
            f"[反应提示]\n{packet.reaction_hint.strip() or '（无）'}",
        ]
        meta_bits: list[str] = []
        if packet.window_title:
            meta_bits.append(f"窗口：{packet.window_title}")
        if packet.app_type:
            meta_bits.append(f"应用类型：{packet.app_type}")
        if packet.content_scene:
            meta_bits.append(f"内容场景：{_scene_label_zh(packet.content_scene)}")
        if packet.visible_text_source:
            meta_bits.append(f"文字采集：{_source_label_zh(packet.visible_text_source)}")
        if packet.process_name:
            meta_bits.append(f"进程：{packet.process_name}")
        if packet.triggers:
            meta_bits.append(f"触发：{', '.join(packet.triggers)}")
        if packet.idle_s > 0:
            meta_bits.append(f"空闲：{packet.idle_s}s")
        if meta_bits:
            parts.insert(0, "[观测元信息]\n" + "\n".join(meta_bits))

        try:
            chat_ctx = self._get_recent_history()
            if chat_ctx:
                parts.append(chat_ctx)
        except Exception:
            pass
        last_spoken = (self._last_spoken_text or "").strip()
        if last_spoken:
            if len(last_spoken) > 160:
                last_spoken = last_spoken[:160] + "…"
            parts.append(
                "[自分の直前の発話]\n"
                f"{last_spoken}\n"
                "※これはあなた（夜乃桜）が先ほど口にした内容。相手の発言ではない。"
            )
        obs_ctx = self._format_obs_history()
        if obs_ctx:
            parts.append(obs_ctx)
        # 决策 LLM 看自己上一轮的短时印象（含「対話の既知事実」），
        # 避免对同一画面/话题重复问。
        obs_impression = sensory_impression_store.get_for_observer()
        if obs_impression:
            parts.append(f"[观察者上下文]\n{obs_impression}")
        if self._relationship_motive:
            parts.append(
                "[关系动机]\n"
                "屏幕事件优先。关系与心情可以作为附加动机，但不要把屏幕内容硬拗成亲密理由，"
                "也不要连续再开一轮关系主动。"
            )
        user_text = "\n\n".join(parts)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]
        return await self._post_speech_decision(messages)

    async def _post_speech_decision(self, messages: list[dict]) -> dict | None:
        if not self._speech_decision_configured:
            return None

        if self._chat_http is None:
            self._chat_http = httpx.AsyncClient(timeout=self.config.request_timeout)

        url = f"{self._chat_api_base_url}/chat/completions"
        payload = {
            "model": self._chat_api_model,
            "messages": messages,
            "temperature": 0.5,
            "max_tokens": 1024,
            "thinking": {"type": "disabled"},
        }
        headers = {
            "Authorization": f"Bearer {self._chat_api_key}",
            "Content-Type": "application/json",
        }

        t0 = time.monotonic()
        data: Any = None
        try:
            resp = await self._chat_http.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            choice = data.get("choices", [{}])[0]
            content = choice.get("message", {}).get("content", "")
            finish = choice.get("finish_reason", "")
            ledger = self._ledger_attempt
            if not content:
                _log_speech_decision_outcome("rejected_invalid_output", finish, 0)
                if ledger is not None:
                    ledger.decision_format = "rejected_invalid_output"
                return None
            parsed = _extract_json(content)
            if parsed:
                _log_speech_decision_outcome("valid_json", finish, len(content))
                if ledger is not None:
                    ledger.decision_format = "valid_json"
                return parsed
            adopted = _adopt_plain_dialogue_decision(content)
            if adopted:
                _log_speech_decision_outcome(
                    "adopted_plain_dialogue", finish, len(content)
                )
                if ledger is not None:
                    ledger.decision_format = "adopted_plain_dialogue"
                return adopted
            _log_speech_decision_outcome(
                "rejected_invalid_output", finish, len(content)
            )
            if ledger is not None:
                ledger.decision_format = "rejected_invalid_output"
            return None
        except Exception as e:
            logger.warning(
                "ProactiveObserver: LLM speech decision call failed: {} ({})",
                e,
                type(e).__name__,
            )
            return None
        finally:
            ledger = self._ledger_attempt
            if ledger is not None:
                ledger.decision_elapsed_ms = int((time.monotonic() - t0) * 1000)
                usage = _extract_openai_usage(data)
                if usage:
                    ledger.decision_usage = usage

    async def _do_evaluation(self, triggers: list[str]) -> None:
        now = time.monotonic()
        self._last_eval_at = now
        attempt = _ObserverLedgerAttempt(
            path="screen",
            source="screen",
            vlm_model=self._api_model,
            decision_model=self._chat_api_model if self._speech_decision_configured else "",
            triggers=tuple(triggers),
        )
        previous_ledger = self._ledger_attempt
        self._ledger_attempt = attempt
        try:
            await self._screen_evaluation_attempt(triggers, now, attempt)
        finally:
            attempt.settle("silent")
            self._ledger_attempt = previous_ledger

    async def _screen_evaluation_attempt(
        self,
        triggers: list[str],
        now: float,
        attempt: _ObserverLedgerAttempt,
    ) -> None:
        # 双重护栏：评估瞬间前台若已切回本进程，直接放弃（截图/UIA 都会读到自家 UI）。
        focus = self._focus_current
        if focus is not None and focus.is_own_process:
            logger.info("ProactiveObserver: skip eval, focus is own process")
            _observer_gui_log("跳过评估：前台为本进程", {"triggers": triggers})
            self._safe_on_evaluate("前台为本进程，跳过", False)
            attempt.settle("preflight_skip")
            return
        try:
            if int(get_active_window_pid() or 0) == int(os.getpid()):
                logger.info("ProactiveObserver: skip eval, foreground pid is self")
                _observer_gui_log("跳过评估：前台为本进程", {"triggers": triggers})
                self._safe_on_evaluate("前台为本进程，跳过", False)
                attempt.settle("preflight_skip")
                return
        except Exception:
            pass

        blocked, matched = self.privacy.check_active_window()
        if blocked:
            logger.info("ProactiveObserver: privacy block ({})", matched)
            _observer_gui_log("隐私拦截", {"matched": matched})
            self._safe_on_evaluate(f"隐私拦截：{matched}", False)
            attempt.settle("preflight_skip")
            return

        try:
            obs = self.capture.grab()
        except Exception as e:
            logger.warning("ProactiveObserver: screen capture failed: {}", e)
            _observer_gui_log("截图失败", {"error": str(e)})
            self._safe_on_evaluate(f"截图失败：{e}", False)
            attempt.settle("capture_error")
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
                attempt.settle("dedup_skip")
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

        # 短时印象（LLM→VLM / 主对话共享）：过期由 store TTL 处理
        observer_ctx = sensory_impression_store.get_for_observer(now=now)
        if observer_ctx:
            ctx_parts.append(f"[观察者上下文]\n{observer_ctx}")

        # 最近观测历史（VLM 用于避免对相似场景写重复摘要）
        # 完整对话历史留给 LLM 决策；VLM 只通过 situational_summary 里的
        # 「対話の既知事実」拿极薄锚点，避免再塞全文。
        obs_ctx = self._format_obs_history()
        if obs_ctx:
            ctx_parts.append(obs_ctx)

        uia_raw = (
            window_text.text_content.strip()
            if window_text.is_accessible
            else ""
        )
        uia_enough = len(uia_raw) >= self.config.content_min_chars
        # 全文 UIA 不喂给 VLM：只告知系统文字是否可用，避免多模态上下文吞长文本。
        if uia_enough:
            ctx_parts.append(
                "[系统文字]：可用（UIA 已读取；后段决策 LLM 会直接读精炼摘录。"
                "请将 on_screen_text 留空，不要抄 UI 字。）"
            )
        else:
            ctx_parts.append(
                "[系统文字]：不可用（请尽量把画面上可读的关键短句填到 on_screen_text；"
                "没有可读文字则留空。）"
            )

        ocr_text = ""
        # 游戏态 OCR：已尝试启用过，WinRT OCR 在游戏窗口上超时/误识别较多、收益不稳定，
        # 故硬性停用（不止是 config 默认关，避免误开又踩坑）。若未来重新启用，把
        # _GAME_OCR_HARD_DISABLED 改为 False 即可，此时 config.game_ocr_enabled 才生效。
        # OCR 结果只进决策 LLM 摘录通道，不喂给 VLM。
        if not _GAME_OCR_HARD_DISABLED and (
            self.config.game_ocr_enabled
            and not uia_enough
            and self._looks_like_game_context(window_text)
        ):
            _observer_gui_log("开始游戏态 OCR")
            ocr_text = await asyncio.to_thread(_ocr_game_dialogue_isolated)
            if ocr_text:
                proc = get_active_window_process_name()
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
            try:
                old_chat = self._chat_http
                self._chat_http = None
                if old_chat:
                    await old_chat.aclose()
            except Exception:
                pass
            attempt.settle("vlm_error")
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
            attempt.settle("vlm_error")
            return

        visual_summary = str(parsed.get("visual_summary", "")).strip()
        reaction_hint = str(parsed.get("reaction_hint", "")).strip()
        on_screen_text = str(parsed.get("on_screen_text", "")).strip()
        # 兼容旧 VLM 仍返回 inner_thought 的情况
        if not visual_summary and not reaction_hint:
            legacy = str(parsed.get("inner_thought", "")).strip()
            if legacy:
                reaction_hint = legacy

        suggested = parsed.get("suggested_interval")
        clamped = self._clamp_suggested_interval(suggested)
        if clamped is not None:
            self._next_timer_at = time.monotonic() + clamped
            logger.debug(
                "ProactiveObserver: adaptive interval set to {:.0f}s (requested {!r})",
                clamped,
                suggested,
            )
        else:
            # 未给建议时回退默认 timer_seconds，周期与同窗冷却仍可对齐
            clamped = self._clamp_suggested_interval(self.config.timer_seconds)
            self._next_timer_at = (
                time.monotonic() + clamped if clamped is not None else 0.0
            )
        # 不论开不开口：压住 content，避免动态网页滚字把评估打成一分钟一轮。
        self._arm_content_quiet(clamped)

        # 同窗口切焦再评：跟本次 suggested_interval（或默认 timer）
        if self._focus_current is not None and clamped is not None:
            self._window_eval_ok_at[self._focus_current.app_key] = (
                time.monotonic() + clamped
            )

        process_name = (
            get_active_window_process_name()
            or window_text.process_name
            or ""
        )
        resolved = resolve_visible_text(
            uia_text=uia_raw,
            ocr_text=ocr_text,
            on_screen_text=on_screen_text if not uia_enough else "",
            min_chars=self.config.content_min_chars,
        )
        # UIA 不够时，VLM 抄的屏上字即使短于 content_min_chars 也应收录
        visible_excerpt = resolved.text
        visible_source = resolved.source
        if not visible_excerpt and on_screen_text and not uia_enough:
            visible_excerpt = on_screen_text[:1200]
            visible_source = "vlm_on_screen"
        content_scene = infer_content_scene(
            window_text.app_type or "",
            process_name,
            window_title or "",
        )

        packet = ObservationPacket(
            window_title=window_title or "",
            app_type=window_text.app_type or "",
            process_name=process_name,
            triggers=tuple(triggers),
            idle_s=idle_s,
            visual_summary=visual_summary,
            reaction_hint=reaction_hint,
            visible_text_excerpt=visible_excerpt,
            visible_text_source=visible_source,
            content_scene=content_scene,
            suggested_interval=clamped,
        )

        if not packet.has_perception:
            logger.info("ProactiveObserver: VLM returned empty perception packet")
            self._safe_on_evaluate("VLM 观测包为空", False)
            attempt.settle("empty_perception")
            return

        logger.info(
            "ProactiveObserver: packet summary={!r} reaction={!r} excerpt_chars={}",
            packet.visual_summary[:80],
            packet.reaction_hint[:80],
            len(packet.visible_text_excerpt),
        )
        _observer_gui_log(
            "观测包已组装",
            {
                "visual_summary": packet.visual_summary[:120],
                "reaction_hint": packet.reaction_hint[:80],
                "excerpt_chars": len(packet.visible_text_excerpt),
                "visible_text_source": packet.visible_text_source,
                "content_scene": packet.content_scene,
                "uia_enough": uia_enough,
            },
        )

        # ---- Stage 2: LLM decides whether to speak ----
        speech_decision: dict | None = None
        skip_speech = self._should_skip_speech_decision(packet, window_title or "")
        # 先判定再更新，避免「本轮摘要」把自己比成相同
        self._last_visual_summary = packet.visual_summary
        self._last_eval_window_title = window_title or ""
        if skip_speech:
            reason = "同窗画面无实质变化，跳过发言决策"
            logger.info("ProactiveObserver: silent (reason: {})", reason)
            _observer_gui_log(reason)
            self._mark_silent_eval()
            self._safe_on_evaluate(reason, False)
            self._record_observation(window_title, False, reason)
            attempt.settle("dedup_skip")
            return
        if self._speech_decision_configured:
            _observer_gui_log(
                "正在调用语言模型决定发言",
                {"model": self._chat_api_model},
            )
            speech_decision = await self._decide_speech(packet)
        else:
            logger.warning(
                "ProactiveObserver: LLM speech decision not configured, falling back to silent"
            )

        # 短时印象：下次 VLM + 主对话共享（不说不进聊天历史）
        if speech_decision is not None:
            summary = str(speech_decision.get("situational_summary", "")).strip()
            if summary:
                spoke = bool(speech_decision.get("should_speak"))
                sensory_impression_store.update(
                    summary,
                    spoken=spoke,
                    window_hint=window_title or "",
                    now=time.monotonic(),
                )
                logger.debug(
                    "ProactiveObserver: sensory impression updated: {}",
                    summary[:120],
                )

        if speech_decision is None:
            reason = "LLM 发言决策失败或未配置"
            logger.info("ProactiveObserver: silent (reason: {})", reason)
            self._mark_silent_eval()
            self._safe_on_evaluate(reason, False)
            self._record_observation(window_title, False, reason)
            attempt.settle("decision_error")
            return

        if not speech_decision.get("should_speak"):
            reason = str(speech_decision.get("reason", "")).strip() or "LLM 选择不发言"
            logger.info("ProactiveObserver: silent (reason: {})", reason)
            self._mark_silent_eval()
            self._safe_on_evaluate(reason, False)
            self._record_observation(window_title, False, reason)
            return

        comment = str(speech_decision.get("comment", "")).strip()
        if not comment:
            reason = "should_speak=true 但 comment 为空"
            logger.warning("ProactiveObserver: {}", reason)
            self._mark_silent_eval()
            self._safe_on_evaluate(reason, False)
            self._record_observation(window_title, False, reason)
            return

        reason = (
            str(speech_decision.get("reason", "")).strip()
            or f"观测: {packet.log_preview[:80]}..."
        )
        self._safe_on_evaluate(reason, True)

        self._last_proactive_at = time.monotonic()
        self._last_silent_eval_at = 0.0
        self._idle_armed = True

        self._record_observation(window_title, True, reason, comment)

        payload = ProactiveSpeakPayload(
            text=comment,
            translation=str(speech_decision.get("translation", "")).strip(),
            tone=str(speech_decision.get("tone", "")).strip() or "中性",
        )
        self._last_spoken_text = comment

        try:
            self.on_speak(payload)
        except Exception as e:
            logger.warning("ProactiveObserver: on_speak callback error: {}", e)
            _observer_gui_log("主动发言回调失败", {"error": str(e)})
        attempt.settle("speak")

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
            "thinking": {"type": "disabled"},
        }

        t0 = time.monotonic()
        data: Any = None
        try:
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
        finally:
            ledger = self._ledger_attempt
            if ledger is not None:
                ledger.vlm_elapsed_ms = int((time.monotonic() - t0) * 1000)
                usage = _extract_openai_usage(data)
                if usage:
                    ledger.vlm_usage = usage


@dataclass
class _ObserverLedgerAttempt:
    """Exactly-once settlement record for one screen or relationship evaluation."""

    path: str
    source: str
    vlm_model: str = ""
    decision_model: str = ""
    triggers: tuple[str, ...] | None = None
    started_at: float = field(default_factory=time.monotonic)
    decision_format: str = ""
    vlm_elapsed_ms: int | None = None
    decision_elapsed_ms: int | None = None
    vlm_usage: dict[str, int] | None = None
    decision_usage: dict[str, int] | None = None
    _settled: bool = False

    def settle(self, outcome: str) -> None:
        if self._settled:
            return
        self._settled = True
        data: dict[str, Any] = {
            "path": self.path,
            "source": self.source,
            "outcome": outcome,
            "total_elapsed_ms": int((time.monotonic() - self.started_at) * 1000),
        }
        if self.path == "screen":
            if self.vlm_model:
                data["vlm_model"] = self.vlm_model
            if self.triggers is not None:
                data["triggers"] = list(self.triggers)
        if self.decision_model:
            data["decision_model"] = self.decision_model
        if self.decision_format:
            data["decision_format"] = self.decision_format
        if self.vlm_elapsed_ms is not None:
            data["vlm_elapsed_ms"] = self.vlm_elapsed_ms
        if self.decision_elapsed_ms is not None:
            data["decision_elapsed_ms"] = self.decision_elapsed_ms
        if self.vlm_usage:
            data["vlm_usage"] = dict(self.vlm_usage)
        if self.decision_usage:
            data["decision_usage"] = dict(self.decision_usage)
        debug_log("ObserverLedger", "评估结算", data)


def _extract_openai_usage(data: Any) -> dict[str, int] | None:
    """Copy provider usage fields only; never estimate missing counts."""
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    copied: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            copied[key] = value
        elif isinstance(value, float) and value.is_integer():
            copied[key] = int(value)
    return copied or None


_PLAIN_DIALOGUE_MAX_CHARS = 80
_PLAIN_DIALOGUE_MAX_SENTENCES = 2
_PLAIN_DIALOGUE_KANA_RE = re.compile(r"[\u3040-\u30ff\uff66-\uff9f]")
_PLAIN_DIALOGUE_SENTENCE_RE = re.compile(r"[。！？!?]+")
_PLAIN_DIALOGUE_JSONISH_RE = re.compile(
    r"[{}\[\]]|\"[A-Za-z_][A-Za-z0-9_]*\"\s*:"
)
_PLAIN_DIALOGUE_LATIN_WORD_RE = re.compile(r"[A-Za-z]{3,}")
_PLAIN_DIALOGUE_MARKDOWN_RE = re.compile(
    r"```|\*\*|__|^\s{0,3}(?:[-*+] |\d+\. |#{1,6} |> )",
    re.MULTILINE,
)
_PLAIN_DIALOGUE_REPORT_MARKERS = (
    "观察者",
    "评估",
    "系统",
    "报告",
    "建议保持",
    "システム",
    "報告",
    "評価",
    "should_speak",
    "ことにします",
    "ことにしました",
    "発言します",
    "発言すること",
    "発言すべき",
    "すべきです",
    "判断しました",
    "判断します",
)


def _log_speech_decision_outcome(outcome: str, finish: object, chars: int) -> None:
    logger.info(
        "ProactiveObserver: speech decision outcome={} finish={} chars={}",
        outcome,
        finish,
        chars,
    )


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


def _is_plain_japanese_dialogue(text: str) -> bool:
    if not text or len(text) > _PLAIN_DIALOGUE_MAX_CHARS:
        return False
    if _PLAIN_DIALOGUE_MARKDOWN_RE.search(text):
        return False
    if _PLAIN_DIALOGUE_JSONISH_RE.search(text):
        return False
    if _PLAIN_DIALOGUE_LATIN_WORD_RE.search(text):
        return False
    if not _PLAIN_DIALOGUE_KANA_RE.search(text):
        return False
    if any(marker in text for marker in _PLAIN_DIALOGUE_REPORT_MARKERS):
        return False
    sentences = [
        part.strip()
        for part in _PLAIN_DIALOGUE_SENTENCE_RE.split(text)
        if part.strip()
    ]
    return 1 <= len(sentences) <= _PLAIN_DIALOGUE_MAX_SENTENCES


def _adopt_plain_dialogue_decision(content: str) -> dict | None:
    comment = (content or "").strip()
    if not _is_plain_japanese_dialogue(comment):
        return None
    return {
        "should_speak": True,
        "comment": comment,
        "translation": "",
        "tone": "中性",
        "reason": "决策输出回退为短日语对白",
        "situational_summary": "",
    }
