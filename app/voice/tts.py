from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from PySide6.QtCore import QObject, QTimer, QUrl, Signal, Slot

from app.core.resource_manager import ResourceManager
from app.core.debug_log import debug_log
from app.core.interaction import get_interaction_id
from app.storage.paths import StoragePaths
from app.voice.tts_settings import (
    GPTSoVITSTTSSettings as _GPTSoVITSTTSSettings,
    TTS_PLAYBACK_BACKEND_AUDIO_SINK as _TTS_PLAYBACK_BACKEND_AUDIO_SINK,
)
from app.voice import audio_checks as _audio_checks
from app.voice.tts_types import (
    TTSCallback,
    TTSPreparedAudio,
    TTSServiceState,
    _parse_service_endpoint,
    _provider_is_closed,
    _set_service_state,
    _TTSRequest,
)
# 服务监督已抽到 tts_service.py、合成队列已抽到 tts_synthesis.py。
# 这里 re-export 供既有测试/装配从 app.voice.tts 导入。
from app.voice.tts_service import (  # noqa: F401
    GenieServiceSupervisor,
    TTSServiceSupervisor,
    _AttachedLocalProcess,
    _LocalProcessHandle,
    _build_genie_endpoint_url,
    _build_genie_start_command,
    _build_gpt_sovits_start_command,
    _build_tts_endpoint_url,
    _encode_genie_character_name,
    _find_running_local_tts_process,
    _format_gpt_sovits_http_error,
    _is_restartable_local_tts_service_failure,
    _is_soft_synth_failure,
    _local_tts_service_log_path,
    _local_tts_subprocess_env,
    _read_local_tts_output,
    _wait_local_service_ready,
)
from app.voice.tts_synthesis import (  # noqa: F401
    GenieSynthesisEngine,
    GPTSoVITSSynthesisEngine,
    TTSSynthesisQueue,
    _is_voiceable_text,
    _resolve_request_text_lang,
    _write_genie_audio,
    _write_raw_float_or_pcm_as_wav,
    _write_raw_pcm_as_wav,
)

if TYPE_CHECKING:
    from PySide6.QtMultimedia import QAudioOutput as QAudioOutputType
    from PySide6.QtMultimedia import QMediaPlayer as QMediaPlayerType

    from app.voice.audio_sink_player import AudioSinkPlayer

QAudioOutput: type[Any] | None = None
QMediaPlayer: type[Any] | None = None

# 默认使用 AudioSink 后端
_DEFAULT_PLAYBACK_BACKEND = _TTS_PLAYBACK_BACKEND_AUDIO_SINK

_AUDIO_CLEANUP_DELAY_MS = 5000
_AUDIO_CLEANUP_MAX_ATTEMPTS = 5
_AUDIO_FINISH_FALLBACK_GRACE_MS = 1500
_AUDIO_FINISH_FALLBACK_MIN_MS = 2000
# 播放完成兜底的上限：时长无法解析或异常超长时按此值兜底，防止流程永久挂起
_AUDIO_FINISH_FALLBACK_MAX_MS = 60_000


def _load_qt_multimedia() -> tuple[type[Any], type[Any]]:
    global QAudioOutput, QMediaPlayer
    if QAudioOutput is None or QMediaPlayer is None:
        from PySide6.QtMultimedia import QAudioOutput as _QAudioOutput
        from PySide6.QtMultimedia import QMediaPlayer as _QMediaPlayer

        QAudioOutput = _QAudioOutput
        QMediaPlayer = _QMediaPlayer
    return QAudioOutput, QMediaPlayer


def _create_audio_sink_player(parent: QObject) -> "AudioSinkPlayer":
    from app.voice.audio_sink_player import AudioSinkPlayer

    return AudioSinkPlayer(parent)


def _resolve_project_root(base_dir: Path | None = None) -> Path:
    """解析项目根目录；base_dir 为空时基于 __file__ 推算（app/voice/tts.py → 项目根），
    与 main.py 的路径惯例一致。"""
    return Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[2]


def _resolve_tts_cache_dir(base_dir: Path | None = None) -> Path:
    """返回 TTS 临时音频缓存目录（data/cache/tts），并确保存在。

    不再写入系统 Temp，改用 Sakura 自有数据目录，便于集中管理与启动清理。
    """
    cache_dir = StoragePaths(_resolve_project_root(base_dir)).tts_cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def purge_tts_cache(base_dir: Path | None = None) -> None:
    """启动时清空 data/cache/tts 残留（崩溃/强退遗留的临时 wav）。

    该目录完全归 Sakura 所有、仅存放 TTS 临时音频，清空安全。
    逐个删除并忽略个别占用错误，不影响启动。
    """
    cache_dir = _resolve_tts_cache_dir(base_dir)
    for entry in cache_dir.iterdir():
        if not entry.is_file():
            continue
        try:
            entry.unlink()
        except OSError as exc:
            debug_log("TTS", "启动清理缓存文件失败，已跳过", {"path": str(entry), "error": str(exc)})


class TTSProvider(Protocol):
    @property
    def service_ready(self) -> bool:
        """本地 TTS 服务是否已探测/预热完成。"""
        ...

    def speak(
        self,
        text: str,
        tone: str | None = None,
        on_finished: TTSCallback | None = None,
        on_started: TTSCallback | None = None,
    ) -> None:
        """播放或提交一段待朗读文本。"""

    def prepare(self, text: str, tone: str | None = None) -> TTSPreparedAudio:
        """提前生成一段待朗读音频，但不立即播放。"""

    def speak_prepared(
        self,
        handle: TTSPreparedAudio,
        on_started: TTSCallback | None = None,
        on_finished: TTSCallback | None = None,
    ) -> None:
        """播放 prepare 返回的音频；若仍在生成，则等待生成完成后播放。"""

    def discard_prepared(self, handle: TTSPreparedAudio) -> None:
        """丢弃不再需要的预生成音频。"""

    def warm_up_playback(self) -> None:
        """提前初始化本地播放器，避免第一句朗读承担冷启动成本。"""

    def ensure_ready(self) -> tuple[bool, str]:
        """同步检测并预热 TTS 服务，不生成或播放音频。"""

    def close(self) -> None:
        """释放 Provider 自己启动的本地服务。"""


class NullTTSProvider:
    @property
    def service_ready(self) -> bool:
        return False

    def speak(
        self,
        text: str,
        tone: str | None = None,
        on_finished: TTSCallback | None = None,
        on_started: TTSCallback | None = None,
    ) -> None:
        # GPT-SoVITS 接入前保留调用点，避免聊天流程以后再改。
        debug_log(
            "TTS",
            "静音 Provider 跳过播放",
            {
                "text": text,
                "tone": tone,
            },
        )
        _ = text
        _ = tone
        if on_started is not None:
            on_started()
        if on_finished is not None:
            on_finished()

    def prepare(self, text: str, tone: str | None = None) -> TTSPreparedAudio:
        debug_log("TTS", "静音 Provider 跳过预生成", {"text": text, "tone": tone})
        return TTSPreparedAudio(text=text.strip(), tone=tone)

    def speak_prepared(
        self,
        handle: TTSPreparedAudio,
        on_started: TTSCallback | None = None,
        on_finished: TTSCallback | None = None,
    ) -> None:
        debug_log(
            "TTS",
            "静音 Provider 跳过预生成播放",
            {
                "text": handle.text,
                "tone": handle.tone,
            },
        )
        _ = handle
        if on_started is not None:
            on_started()
        if on_finished is not None:
            on_finished()

    def discard_prepared(self, handle: TTSPreparedAudio) -> None:
        debug_log("TTS", "丢弃静音预生成句柄", {"text": handle.text, "tone": handle.tone})
        handle.cancelled = True

    def warm_up_playback(self) -> None:
        debug_log("TTS", "静音 Provider 跳过播放器预热")

    def ensure_ready(self) -> tuple[bool, str]:
        debug_log("TTS", "静音 Provider 跳过服务检测")
        return True, "TTS 已关闭。"

    def close(self) -> None:
        debug_log("TTS", "静音 Provider 无需关闭")


class GPTSoVITSTTSProvider(QObject):
    error_occurred = Signal(str)
    _audio_ready = Signal(str, object, object, str)
    _prepared_audio_ready = Signal(object, str)
    _prepared_audio_failed = Signal(object, str)
    _prepared_audio_skipped = Signal(object)
    _failed = Signal(str)
    _started = Signal(object)
    _finished = Signal(object)

    def __init__(
        self,
        settings: _GPTSoVITSTTSSettings,
        *,
        base_dir: Path | None = None,
        adopt_existing_service: bool = True,
    ) -> None:
        super().__init__()
        settings.validate()
        # TTS 临时音频缓存目录（data/cache/tts）。由调用方注入 base_dir，
        # 与启动清理 purge_tts_cache(base_dir) 同源，避免写入目录与清理目录错位。
        # base_dir 为空时退回 _resolve_tts_cache_dir 的 __file__ 推算，保持向后兼容。
        self._base_dir = Path(base_dir) if base_dir is not None else None
        self._tts_cache_dir = _resolve_tts_cache_dir(base_dir)
        # 队列元素：(音频路径, 开始回调, 完成回调, 预生成句柄, 合成文本)
        self._pending_audio: list[
            tuple[Path, TTSCallback | None, TTSCallback | None, TTSPreparedAudio | None, str]
        ] = []
        self._current_audio: Path | None = None
        # 当前正在播放的音频对应的合成文本，仅用于日志展示
        self._current_text: str = ""
        self._current_started: TTSCallback | None = None
        self._current_finished: TTSCallback | None = None
        self._current_started_emitted = False
        self._finishing_audio = False
        # 关闭标志由 _close_lock 守护（合成队列在自己的锁内不持有它，避免反向锁序）。
        self._close_lock = threading.Lock()
        self._closed = False
        self._playback_warmup_requested = False
        self._playback_finish_token = 0
        # 播放后端：audio_sink 或 media_player
        self._playback_backend: str = (
            getattr(settings, "playback_backend", _DEFAULT_PLAYBACK_BACKEND)
            or _DEFAULT_PLAYBACK_BACKEND
        )
        self._sink_player: AudioSinkPlayer | None = None

        self._audio_output: QAudioOutputType | None = None
        self._player: QMediaPlayerType | None = None
        # 协调器自持一个 ResourceManager：本地子进程 + 合成线程都注册进去，
        # close() 走 stop_all 统一关闭；provider 退役/热切换沿用 close()，无需共享 RM。
        self._resource_manager = ResourceManager(self)
        # 服务进程监督拆到 TTSServiceSupervisor；settings 由 supervisor 持有（见 settings 属性）。
        self._supervisor = self._create_supervisor(
            settings, adopt_existing_service=adopt_existing_service
        )
        # 合成队列拆到 TTSSynthesisQueue；以本协调器为 sink 把结果投回播放队列。
        self._synthesis_queue = self._create_synthesis_queue()
        self._audio_ready.connect(self._enqueue_audio)
        self._prepared_audio_ready.connect(self._store_prepared_audio)
        self._prepared_audio_failed.connect(self._fail_prepared_audio)
        self._prepared_audio_skipped.connect(self._skip_prepared_audio)
        self._failed.connect(self._log_error)
        self._started.connect(self._run_callback)
        self._finished.connect(self._run_callback)

    def _create_supervisor(
        self,
        settings: _GPTSoVITSTTSSettings,
        *,
        adopt_existing_service: bool,
    ) -> TTSServiceSupervisor:
        """装配本 Provider 的服务监督；Genie 子类覆写为 GenieServiceSupervisor。"""
        return TTSServiceSupervisor(
            settings,
            base_dir=self._base_dir,
            resource_manager=self._resource_manager,
            is_closed=self._is_closed,
            adopt_existing_service=adopt_existing_service,
        )

    def _create_synthesis_engine(self) -> object:
        """装配本 Provider 的合成引擎；Genie 子类覆写为 GenieSynthesisEngine。"""
        return GPTSoVITSSynthesisEngine()

    def _create_synthesis_queue(self) -> TTSSynthesisQueue:
        return TTSSynthesisQueue(
            supervisor=self._supervisor,
            engine=self._create_synthesis_engine(),
            cache_dir=self._tts_cache_dir,
            resource_manager=self._resource_manager,
            sink=self,
            is_closed=self._is_closed,
        )

    # ---- TTSSynthesisSink：合成结果交回播放队列（commit 5 前由协调器实现） ----

    def deliver_audio(
        self,
        audio_path: str,
        on_started: TTSCallback | None,
        on_finished: TTSCallback | None,
        text: str,
    ) -> None:
        self._audio_ready.emit(audio_path, on_started, on_finished, text)

    def deliver_prepared(self, handle: TTSPreparedAudio, audio_path: str) -> None:
        self._prepared_audio_ready.emit(handle, audio_path)

    def schedule_cleanup(self, audio_path: Path) -> None:
        self._schedule_audio_cleanup(audio_path)

    @property
    def settings(self) -> _GPTSoVITSTTSSettings:
        """settings 由 supervisor 持有，使 Genie 备用端口切换能传播到合成路径。"""
        return self._supervisor.settings

    @property
    def service_ready(self) -> bool:
        """服务探测是否已成功(实际可达)，委托给服务监督。

        供接话音频预生成等调用方做就绪门控:provider 实例存在不代表
        服务已启动,未就绪时发起 prepare 只会得到静默失败。
        """
        return self._supervisor.service_ready

    def speak(
        self,
        text: str,
        tone: str | None = None,
        on_finished: TTSCallback | None = None,
        on_started: TTSCallback | None = None,
    ) -> None:
        text = text.strip()
        if not text:
            debug_log("TTS", "空文本跳过播放")
            self._started.emit(on_started)
            self._finished.emit(on_finished)
            return
        debug_log("TTS", "提交播放请求", {"text": text, "tone": tone})
        self._synthesis_queue.submit(
            _TTSRequest(
                text=text,
                tone=tone,
                on_started=on_started,
                on_finished=on_finished,
                interaction_id=get_interaction_id(),
            )
        )

    def prepare(self, text: str, tone: str | None = None) -> TTSPreparedAudio:
        text = text.strip()
        handle = TTSPreparedAudio(text=text, tone=tone)
        if not text:
            debug_log("TTS", "空文本跳过预生成")
            handle.failed = True
            return handle
        debug_log("TTS", "提交预生成请求", {"text": text, "tone": tone})
        self._synthesis_queue.submit(
            _TTSRequest(
                text=text,
                tone=tone,
                prepared_audio=handle,
                interaction_id=get_interaction_id(),
            )
        )
        return handle

    def speak_prepared(
        self,
        handle: TTSPreparedAudio,
        on_started: TTSCallback | None = None,
        on_finished: TTSCallback | None = None,
    ) -> None:
        if handle.cancelled:
            debug_log("TTS", "预生成句柄已取消，跳过播放", {"text": handle.text, "tone": handle.tone})
            self._started.emit(on_started)
            self._finished.emit(on_finished)
            return
        if not handle.text or handle.failed:
            debug_log(
                "TTS",
                "预生成句柄不可播放，直接完成",
                {
                    "text": handle.text,
                    "tone": handle.tone,
                    "failed": handle.failed,
                },
            )
            self._started.emit(on_started)
            self._finished.emit(on_finished)
            return
        handle.play_requested = True
        handle.on_started = on_started
        handle.on_finished = on_finished
        debug_log(
            "TTS",
            "请求播放预生成音频",
            {
                "text": handle.text,
                "tone": handle.tone,
                "audio_ready": handle.audio_path is not None,
            },
        )
        if handle.audio_path is not None:
            self._enqueue_prepared_audio(handle)

    def discard_prepared(self, handle: TTSPreparedAudio) -> None:
        handle.cancelled = True
        debug_log("TTS", "取消预生成音频", {"text": handle.text, "tone": handle.tone})
        self._synthesis_queue.discard_pending(handle)

        pending_audio: list[
            tuple[Path, TTSCallback | None, TTSCallback | None, TTSPreparedAudio | None, str]
        ] = []
        for audio_path, on_started, on_finished, prepared_audio, text in self._pending_audio:
            if prepared_audio is handle:
                self._schedule_audio_cleanup(audio_path)
                continue
            pending_audio.append((audio_path, on_started, on_finished, prepared_audio, text))
        self._pending_audio = pending_audio

        if handle.audio_path is not None:
            self._schedule_audio_cleanup(handle.audio_path)
            handle.audio_path = None

    def warm_up_playback(self) -> None:
        """把 Qt Multimedia 的冷启动提前到空闲阶段完成。"""

        if self._player is not None:
            debug_log("TTS", "Qt 多媒体播放器已初始化，跳过预热")
            return
        if self._playback_warmup_requested:
            debug_log("TTS", "Qt 多媒体播放器预热已排队，跳过重复请求")
            return
        self._playback_warmup_requested = True
        debug_log("TTS", "安排 Qt 多媒体播放器预热")
        QTimer.singleShot(0, self._warm_up_playback)

    @Slot()
    def _warm_up_playback(self) -> None:
        started_at = time.perf_counter()
        try:
            if self._player is not None:
                debug_log("TTS", "Qt 多媒体播放器已初始化，预热无需执行")
                return
            debug_log("TTS", "开始预热 Qt 多媒体播放器")
            self._ensure_player()
            debug_log(
                "TTS",
                "Qt 多媒体播放器预热完成",
                {"elapsed_ms": int((time.perf_counter() - started_at) * 1000)},
            )
        except Exception as exc:  # noqa: BLE001
            debug_log("TTS", "Qt 多媒体播放器预热失败", {"error": str(exc)})
            self._failed.emit(f"Qt 多媒体播放器预热失败：{exc}")
        finally:
            self._playback_warmup_requested = False

    def ensure_ready(self) -> tuple[bool, str]:
        """同步检测并预热本地 TTS 服务，委托给服务监督。"""
        return self._supervisor.ensure_ready()


    @Slot(str, object, object)
    def _enqueue_audio(
        self,
        audio_path: str,
        on_started: TTSCallback | None,
        on_finished: TTSCallback | None,
        text: str = "",
    ) -> None:
        if _provider_is_closed(self):
            path = Path(audio_path)
            debug_log("TTS", "Provider 已关闭，清理迟到音频", {"audio_path": path, "text": text})
            self._schedule_audio_cleanup(path)
            return
        self._pending_audio.append((Path(audio_path), on_started, on_finished, None, text))
        debug_log(
            "TTS",
            "音频加入播放队列",
            {
                "text": text,
                "audio_path": audio_path,
                "pending_audio": len(self._pending_audio),
                "current_audio": str(self._current_audio) if self._current_audio else None,
                "playback_state": self._playback_backend,
            },
        )
        if self._current_audio is None:
            QTimer.singleShot(0, self._play_next)

    @Slot(object, str)
    def _store_prepared_audio(self, handle: TTSPreparedAudio, audio_path: str) -> None:
        path = Path(audio_path)
        if _provider_is_closed(self):
            handle.failed = True
            debug_log("TTS", "Provider 已关闭，清理迟到的预生成音频", {"audio_path": path})
            self._schedule_audio_cleanup(path)
            return
        if handle.cancelled:
            debug_log("TTS", "预生成音频已取消，清理文件", {"audio_path": path})
            self._schedule_audio_cleanup(path)
            return
        handle.audio_path = path
        debug_log(
            "TTS",
            "预生成音频已就绪",
            {
                "text": handle.text,
                "tone": handle.tone,
                "audio_path": path,
                "play_requested": handle.play_requested,
            },
        )
        if handle.play_requested:
            self._enqueue_prepared_audio(handle)

    @Slot(object, str)
    def _fail_prepared_audio(self, handle: TTSPreparedAudio, message: str) -> None:
        if _provider_is_closed(self):
            handle.failed = True
            return
        self._log_error(message)
        handle.failed = True
        if handle.cancelled or not handle.play_requested:
            return
        self._started.emit(handle.on_started)
        self._finished.emit(handle.on_finished)
        handle.on_started = None
        handle.on_finished = None

    @Slot(object)
    def _skip_prepared_audio(self, handle: TTSPreparedAudio) -> None:
        """预生成句柄静默失败：标记 failed 并完成回调，但不触发 error_occurred。

        与 _fail_prepared_audio 的唯一区别是不调用 _log_error，因此不会向 UI 报错。
        """
        handle.failed = True
        if handle.cancelled or not handle.play_requested:
            return
        self._started.emit(handle.on_started)
        self._finished.emit(handle.on_finished)
        handle.on_started = None
        handle.on_finished = None

    @Slot(object)
    def _handle_media_status(self, status: object) -> None:
        debug_log(
            "TTS",
            "播放器媒体状态变化",
            {
                "status": str(status),
                "audio_path": str(self._current_audio) if self._current_audio else "",
            },
        )
        _QAudioOutput, QMediaPlayer = _load_qt_multimedia()
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._finish_current_audio("end_of_media")
            self._play_next()

    @Slot(object)
    def _handle_playback_state(self, state: object) -> None:
        debug_log(
            "TTS",
            "播放器播放状态变化",
            {
                "state": str(state),
                "audio_path": str(self._current_audio) if self._current_audio else "",
            },
        )
        _QAudioOutput, QMediaPlayer = _load_qt_multimedia()
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._emit_current_started()
            return
        if (
            state == QMediaPlayer.PlaybackState.StoppedState
            and self._current_audio is not None
            and self._current_started_emitted
        ):
            debug_log(
                "TTS",
                "播放器停止，按当前音频播放完成处理",
                {"audio_path": str(self._current_audio)},
            )
            self._finish_current_audio("stopped_state")
            self._play_next()

    @Slot(object, str)
    def _handle_player_error(self, _error: object, error_text: str) -> None:
        debug_log(
            "TTS",
            "播放器错误",
            {
                "error": error_text,
                "audio_path": str(self._current_audio) if self._current_audio else "",
                "pending_audio": len(self._pending_audio),
            },
        )
        self._log_error(f"音频播放失败：{error_text}")
        self._finish_current_audio("player_error")
        self._play_next()

    @Slot(str)
    def _log_error(self, message: str) -> None:
        debug_log("TTS", "错误通知", {"message": message})
        self.error_occurred.emit(message)

    @Slot(object)
    def _run_callback(self, callback: TTSCallback | None) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception as exc:  # noqa: BLE001
            self._log_error(f"TTS 回调执行失败：{exc}")

    def _fail_request(
        self,
        message: str,
        on_started: TTSCallback | None,
        on_finished: TTSCallback | None,
    ) -> None:
        self._failed.emit(message)
        debug_log("TTS", "音频请求失败", {"message": message})
        self._started.emit(on_started)
        self._finished.emit(on_finished)

    def _fail_audio_request(self, request: _TTSRequest, message: str) -> None:
        if _provider_is_closed(self):
            debug_log("TTS", "Provider 已关闭，忽略音频请求失败通知", {"message": message})
            return
        if request.prepared_audio is None:
            self._fail_request(message, request.on_started, request.on_finished)
            return
        self._prepared_audio_failed.emit(request.prepared_audio, message)

    def _skip_audio_request(self, request: _TTSRequest, reason: str) -> None:
        """本段无需/无法发音但不算故障：正常走完回调让流程推进，不向 UI 报错。

        与 _fail_audio_request 相比，不 emit _failed/error_occurred，只记 debug；
        用于纯标点段（无可发音内容）与服务端单段 tts failed 的优雅降级。
        """
        debug_log("TTS", "跳过本段合成", {"text": request.text, "reason": reason})
        if request.prepared_audio is None:
            self._started.emit(request.on_started)
            self._finished.emit(request.on_finished)
            return
        self._prepared_audio_skipped.emit(request.prepared_audio)

    def _enqueue_prepared_audio(self, handle: TTSPreparedAudio) -> None:
        if _provider_is_closed(self):
            if handle.audio_path is not None:
                self._schedule_audio_cleanup(handle.audio_path)
                handle.audio_path = None
            handle.failed = True
            return
        if handle.cancelled or handle.enqueued or handle.audio_path is None:
            return
        handle.enqueued = True
        self._pending_audio.append(
            (handle.audio_path, handle.on_started, handle.on_finished, handle, handle.text)
        )
        debug_log(
            "TTS",
            "预生成音频加入播放队列",
            {
                "text": handle.text,
                "tone": handle.tone,
                "audio_path": handle.audio_path,
                "pending_audio": len(self._pending_audio),
                "prepared": True,
                "play_requested": handle.play_requested,
                "current_audio": str(self._current_audio) if self._current_audio else None,
            },
        )
        handle.audio_path = None
        if self._current_audio is None:
            QTimer.singleShot(0, self._play_next)

    def _play_next(self) -> None:
        """从播放队列取下一段音频并播放，根据后端配置分发。"""
        if _provider_is_closed(self):
            self._clear_pending_audio()
            return
        if self._current_audio is not None or not self._pending_audio:
            return
        (
            audio_path,
            on_started,
            on_finished,
            _prepared_audio,
            text,
        ) = self._pending_audio.pop(0)
        self._current_audio = audio_path
        self._current_text = text
        self._current_started = on_started
        self._current_finished = on_finished
        self._current_started_emitted = False
        self._playback_finish_token += 1

        debug_log(
            "TTS",
            "开始播放音频",
            {
                "text": text,
                "backend": self._playback_backend,
                "audio_path": str(audio_path),
                "file_size": audio_path.stat().st_size if audio_path.exists() else 0,
                "pending_audio": len(self._pending_audio),
            },
        )

        # 播放前最后一道检查：文件可能在排队期间被清理/损坏；
        # 坏条目直接跳过并继续播放队列，绝不交给播放器去卡死
        audio_issue = _audio_checks._verify_generated_audio(audio_path)
        if audio_issue is not None:
            debug_log(
                "TTS",
                "播放前音频校验失败，跳过该条目",
                {"audio_path": str(audio_path), "issue": audio_issue},
            )
            self._finish_current_audio("invalid_audio")
            self._play_next()
            return

        if self._playback_backend == _TTS_PLAYBACK_BACKEND_AUDIO_SINK:
            self._play_next_with_sink()
        else:
            self._play_next_with_media_player()

    def _play_next_with_media_player(self) -> None:
        """旧 QMediaPlayer 播放后端。"""
        audio_path = self._current_audio
        playback_finish_token = self._playback_finish_token
        if audio_path is None:
            return

        self._ensure_player()
        if self._player is None:
            self._fail_audio_playback("播放器初始化失败。")
            return

        self._player.setSource(QUrl.fromLocalFile(str(audio_path)))
        self._player.play()
        self._schedule_current_audio_finish_fallback(
            audio_path,
            playback_finish_token,
        )

    def _play_next_with_sink(self) -> None:
        """QAudioSink 播放后端。"""
        audio_path = self._current_audio
        playback_finish_token = self._playback_finish_token
        if audio_path is None:
            return

        # 销毁旧 sink player
        if self._sink_player is not None:
            try:
                self._sink_player.finished.disconnect()
                self._sink_player.started.disconnect()
                self._sink_player.error.disconnect()
            except Exception:
                pass
            self._sink_player = None

        self._sink_player = _create_audio_sink_player(self)
        self._sink_player.started.connect(self._on_sink_started)
        self._sink_player.finished.connect(self._on_sink_finished)
        self._sink_player.error.connect(self._on_sink_error)

        debug_log(
            "TTS",
            "AudioSink: 尝试启动播放",
            {"audio_path": str(audio_path), "token": playback_finish_token},
        )
        ok = self._sink_player.start(audio_path)
        if not ok:
            # sink 不支持此格式，fallback 到 QMediaPlayer
            debug_log(
                "TTS",
                "AudioSink: fallback 到 QMediaPlayer",
                {
                    "fallback_reason": "sink_start_returned_false",
                    "audio_path": str(audio_path),
                },
            )
            self._sink_player = None
            self._play_next_with_media_player()
            return

        # sink 后端也设置兜底定时器（作为额外安全网）
        self._schedule_current_audio_finish_fallback(
            audio_path,
            playback_finish_token,
        )

    @Slot()
    def _on_sink_started(self) -> None:
        """AudioSinkPlayer 开始播放回调。"""
        debug_log(
            "TTS",
            "AudioSink: 播放开始回调",
            {"audio_path": str(self._current_audio) if self._current_audio else ""},
        )
        self._emit_current_started()

    @Slot(str, str)
    def _on_sink_finished(self, reason: str, audio_path_str: str) -> None:
        """AudioSinkPlayer 播放完成回调。"""
        debug_log(
            "TTS",
            "AudioSink: 播放完成回调",
            {"reason": reason, "audio_path": audio_path_str},
        )
        try:
            self._finish_current_audio(reason)
            self._play_next()
        except Exception as exc:
            debug_log(
                "TTS",
                "AudioSink: 完成回调异常",
                {"error": str(exc), "exception_type": type(exc).__name__},
            )
            self._finish_current_audio("callback_error")
            self._play_next()

    @Slot(str)
    def _on_sink_error(self, message: str) -> None:
        """AudioSinkPlayer 播放错误回调。"""
        debug_log(
            "TTS",
            "AudioSink: 播放错误回调",
            {"error": message, "audio_path": str(self._current_audio) if self._current_audio else ""},
        )
        self._log_error(message)
        try:
            self._finish_current_audio("sink_error")
            self._play_next()
        except Exception as exc:
            debug_log(
                "TTS",
                "AudioSink: 错误回调异常",
                {"error": str(exc), "exception_type": type(exc).__name__},
            )
            self._finish_current_audio("callback_error")
            self._play_next()

    def _ensure_player(self) -> None:
        if self._player is not None:
            return
        QAudioOutput, QMediaPlayer = _load_qt_multimedia()
        self._audio_output = QAudioOutput(self)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio_output)
        self._player.mediaStatusChanged.connect(self._handle_media_status)
        self._player.playbackStateChanged.connect(self._handle_playback_state)
        self._player.errorOccurred.connect(self._handle_player_error)
        debug_log("TTS", "Qt 多媒体播放器已初始化")

    def _fail_audio_playback(self, message: str) -> None:
        audio_path = self._current_audio
        on_started = self._current_started
        on_finished = self._current_finished
        self._reset_current_audio_state()
        if audio_path is not None:
            self._schedule_audio_cleanup(audio_path)
        self._log_error(message)
        self._started.emit(on_started)
        self._finished.emit(on_finished)

    def _emit_current_started(self) -> None:
        if self._current_started_emitted:
            return
        self._current_started_emitted = True
        debug_log("TTS", "音频开始回调", {"audio_path": self._current_audio})
        self._started.emit(self._current_started)

    def _finish_current_audio(self, reason: str = "normal") -> None:
        """统一 finish 入口，保证幂等性。"""
        if self._finishing_audio:
            debug_log(
                "TTS",
                "音频正在 finish 中，跳过重复调用",
                {"reason": reason, "audio_path": str(self._current_audio) if self._current_audio else ""},
            )
            return
        audio_path = self._current_audio
        on_finished = self._current_finished
        if audio_path is None:
            self._reset_current_audio_state()
            return
        self._finishing_audio = True
        try:
            debug_log(
                "TTS",
                "音频播放完成",
                {
                    "text": self._current_text,
                    "reason": reason,
                    "audio_path": str(audio_path),
                    "pending_audio": len(self._pending_audio),
                },
            )
            self._emit_current_started()
            # 停止 sink player（如果正在使用）
            if self._sink_player is not None:
                try:
                    self._sink_player.stop()
                except Exception:
                    pass
                self._sink_player = None
            # 释放 QMediaPlayer（如果正在使用）
            self._release_player_source()
            self._reset_current_audio_state()
            self._schedule_audio_cleanup(audio_path)
            self._finished.emit(on_finished)
        finally:
            self._finishing_audio = False

    def _release_player_source(self) -> None:
        if self._player is None:
            return
        self._player.stop()
        self._player.setSource(QUrl())

    def _reset_current_audio_state(self) -> None:
        self._current_audio = None
        self._current_text = ""
        self._current_started = None
        self._current_finished = None
        self._current_started_emitted = False

    def _schedule_current_audio_finish_fallback(self, audio_path: Path, playback_finish_token: int) -> None:
        duration_ms = _audio_checks._wav_duration_ms(audio_path)
        if duration_ms is None:
            # 时长读不出（文件损坏/被占用）更要兜底——这是播放器最可能卡死的场景；
            # 用保守上限兜住，绝不能因解析失败而放弃兜底导致对话流程挂起
            debug_log(
                "TTS",
                "无法读取音频时长，使用上限时长兜底",
                {"audio_path": audio_path, "delay_ms": _AUDIO_FINISH_FALLBACK_MAX_MS},
            )
            duration_ms = _AUDIO_FINISH_FALLBACK_MAX_MS
        delay_ms = max(
            _AUDIO_FINISH_FALLBACK_MIN_MS,
            min(duration_ms + _AUDIO_FINISH_FALLBACK_GRACE_MS, _AUDIO_FINISH_FALLBACK_MAX_MS),
        )
        debug_log(
            "TTS",
            "安排音频播放完成兜底",
            {
                "audio_path": audio_path,
                "duration_ms": duration_ms,
                "delay_ms": delay_ms,
                "token": playback_finish_token,
            },
        )
        QTimer.singleShot(
            delay_ms,
            lambda path=audio_path, token=playback_finish_token: self._finish_current_audio_if_stalled(
                path,
                token,
            ),
        )

    def _finish_current_audio_if_stalled(self, audio_path: Path, playback_finish_token: int) -> None:
        if playback_finish_token != self._playback_finish_token or self._current_audio != audio_path:
            return
        if self._finishing_audio:
            debug_log(
                "TTS",
                "音频播放完成兜底已过期，跳过",
                {
                    "audio_path": str(audio_path),
                    "token": playback_finish_token,
                },
            )
            return
        debug_log(
            "TTS",
            "音频播放完成事件未触发，使用时长兜底完成",
            {
                "audio_path": str(audio_path),
                "token": playback_finish_token,
                "current_audio": str(self._current_audio) if self._current_audio else "",
            },
        )
        self._finish_current_audio("fallback_timeout")
        self._play_next()

    def _schedule_audio_cleanup(self, audio_path: Path, attempt: int = 1) -> None:
        debug_log("TTS", "计划清理临时音频", {"audio_path": audio_path, "attempt": attempt})
        QTimer.singleShot(
            _AUDIO_CLEANUP_DELAY_MS,
            lambda path=audio_path, current_attempt=attempt: self._cleanup_audio_file(
                path,
                current_attempt,
            ),
        )

    def _cleanup_audio_file(self, audio_path: Path, attempt: int) -> None:
        try:
            audio_path.unlink(missing_ok=True)
            debug_log("TTS", "临时音频清理完成", {"audio_path": audio_path, "attempt": attempt})
        except OSError as exc:
            if attempt < _AUDIO_CLEANUP_MAX_ATTEMPTS:
                self._schedule_audio_cleanup(audio_path, attempt + 1)
                return
            self._log_error(f"临时音频清理失败：{exc}")

    def close(self) -> None:
        with self._close_lock:
            self._closed = True
        self._synthesis_queue.clear_pending()
        self._clear_pending_audio()
        if self._current_audio is not None:
            self._finish_current_audio("provider_closed")
        self._release_player_source()
        # 本地子进程 + 合成线程由协调器自持的 RM 托管，统一经 stop_all 关闭。
        self._resource_manager.stop_all()

    def _is_closed(self) -> bool:
        with self._close_lock:
            return self._closed

    def _clear_pending_audio(self) -> None:
        pending_audio = self._pending_audio
        self._pending_audio = []
        for audio_path, _on_started, _on_finished, _prepared_audio, _text in pending_audio:
            self._schedule_audio_cleanup(audio_path)

    def detach_local_service(self) -> None:
        """交出本地服务进程所有权，供新的 Provider 在后台接管（委托服务监督）。"""
        self._supervisor.detach_local_service()


class GenieTTSProvider(GPTSoVITSTTSProvider):
    """Genie TTS CPU 推理 Provider，复用现有队列、预生成和播放器链路。

    差异收敛到装配选型：服务监督差异（Genie API 探测 / 备用端口 / 角色模型 /
    参考音频 / ONNX 转换）在 GenieServiceSupervisor，合成差异在 GenieSynthesisEngine。
    """

    def _create_supervisor(
        self,
        settings: _GPTSoVITSTTSSettings,
        *,
        adopt_existing_service: bool,
    ) -> TTSServiceSupervisor:
        return GenieServiceSupervisor(
            settings,
            base_dir=self._base_dir,
            resource_manager=self._resource_manager,
            is_closed=self._is_closed,
            adopt_existing_service=adopt_existing_service,
        )

    def _create_synthesis_engine(self) -> object:
        return GenieSynthesisEngine()


