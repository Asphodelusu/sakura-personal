from __future__ import annotations

import json
import re
import socket
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlencode, urlparse, urlunparse

from PySide6.QtCore import QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from app.config.character_loader import CharacterProfile
from app.llm.chat_reply import DEFAULT_TONE
from app.core.debug_log import debug_log


TTSCallback = Callable[[], None]
_AUDIO_CLEANUP_DELAY_MS = 200
_AUDIO_CLEANUP_MAX_ATTEMPTS = 5
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")
_CJK_TEXT_LANGS = {"ja", "all_ja", "zh", "all_zh", "ko", "all_ko", "yue", "all_yue"}


@dataclass
class TTSPreparedAudio:
    """一段已提交预生成的 TTS 音频句柄。"""

    text: str
    tone: str | None = None
    audio_path: Path | None = None
    play_requested: bool = False
    enqueued: bool = False
    cancelled: bool = False
    failed: bool = False
    on_started: TTSCallback | None = None
    on_finished: TTSCallback | None = None


@dataclass(frozen=True)
class _TTSRequest:
    text: str
    tone: str | None
    on_started: TTSCallback | None = None
    on_finished: TTSCallback | None = None
    prepared_audio: TTSPreparedAudio | None = None


class TTSProvider(Protocol):
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


class NullTTSProvider:
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


class TTSConfigError(RuntimeError):
    """TTS 配置缺失或格式错误。"""


@dataclass(frozen=True)
class ToneReference:
    tone: str
    ref_audio_path: Path
    ref_text: str
    ref_lang: str


@dataclass(frozen=True)
class GPTSoVITSTTSSettings:
    enabled: bool
    api_url: str
    ref_audio_path: Path
    ref_text_path: Path
    ref_text: str
    gpt_model_path: Path | None = None
    sovits_model_path: Path | None = None
    ref_lang: str = "ja"
    text_lang: str = "ja"
    timeout_seconds: int = 60
    tone_references: dict[str, list[ToneReference]] = field(default_factory=dict)

    @classmethod
    def from_character_profile(
        cls,
        character_profile: CharacterProfile,
        enabled: bool,
        api_url: str,
        ref_lang: str,
        text_lang: str,
        timeout_seconds: int,
        validate_enabled: bool = True,
    ) -> "GPTSoVITSTTSSettings":
        if character_profile.voice is None:
            settings = cls(
                enabled=enabled,
                api_url=api_url,
                ref_audio_path=character_profile.package_dir,
                ref_text_path=character_profile.package_dir,
                ref_text="",
                ref_lang=ref_lang,
                text_lang=text_lang,
                timeout_seconds=timeout_seconds,
            )
            if enabled and validate_enabled:
                settings.validate()
            return settings

        voice = character_profile.voice
        tone_references = _load_tone_references(
            voice.tone_ref_path,
            character_profile.package_dir,
        )
        neutral_reference = _select_neutral_reference(tone_references)
        settings = cls(
            enabled=enabled,
            api_url=api_url,
            ref_audio_path=neutral_reference.ref_audio_path if neutral_reference else character_profile.package_dir,
            ref_text_path=neutral_reference.ref_audio_path if neutral_reference else character_profile.package_dir,
            ref_text=neutral_reference.ref_text if neutral_reference else "",
            gpt_model_path=voice.gpt_model_path,
            sovits_model_path=voice.sovits_model_path,
            ref_lang=ref_lang,
            text_lang=text_lang,
            timeout_seconds=timeout_seconds,
            tone_references=tone_references,
        )
        if enabled and validate_enabled:
            settings.validate()
        return settings

    def validate(self) -> None:
        if not self.api_url:
            raise TTSConfigError("缺少 GPT_SOVITS_API_URL。")
        if self.gpt_model_path is not None and not self.gpt_model_path.exists():
            raise TTSConfigError(f"GPT 模型不存在：{self.gpt_model_path}")
        if self.sovits_model_path is not None and not self.sovits_model_path.exists():
            raise TTSConfigError(f"SoVITS 模型不存在：{self.sovits_model_path}")
        if self.tone_references:
            for references in self.tone_references.values():
                for reference in references:
                    if not reference.ref_audio_path.exists():
                        raise TTSConfigError(f"语气参考音频不存在：{reference.ref_audio_path}")
                    if not reference.ref_text:
                        raise TTSConfigError(f"语气参考文本为空：{reference.ref_audio_path}")
                    if not reference.ref_lang:
                        raise TTSConfigError(f"语气参考语言为空：{reference.ref_audio_path}")
        else:
            if not self.ref_audio_path.exists():
                raise TTSConfigError(f"参考音频不存在：{self.ref_audio_path}")
            if not self.ref_text:
                raise TTSConfigError("缺少参考文本，请配置 GPT_SOVITS_REF_TEXT 或 GPT_SOVITS_REF_TEXT_PATH。")
        if not self.ref_lang:
            raise TTSConfigError("缺少 GPT_SOVITS_REF_LANG。")
        if not self.text_lang:
            raise TTSConfigError("缺少 GPT_SOVITS_TEXT_LANG。")

class GPTSoVITSTTSProvider(QObject):
    _audio_ready = Signal(str, object, object)
    _prepared_audio_ready = Signal(object, str)
    _prepared_audio_failed = Signal(object, str)
    _failed = Signal(str)
    _started = Signal(object)
    _finished = Signal(object)

    def __init__(self, settings: GPTSoVITSTTSSettings) -> None:
        super().__init__()
        settings.validate()
        self.settings = settings
        self._pending_audio: list[
            tuple[Path, TTSCallback | None, TTSCallback | None, TTSPreparedAudio | None]
        ] = []
        self._current_audio: Path | None = None
        self._current_started: TTSCallback | None = None
        self._current_finished: TTSCallback | None = None
        self._current_started_emitted = False
        self._finishing_audio = False
        self._request_lock = threading.Lock()
        self._pending_requests: list[_TTSRequest] = []
        self._request_running = False
        self._tone_indices: dict[str, int] = {}
        self._weights_ready = False
        self._service_checked = False

        self._audio_output: QAudioOutput | None = None
        self._player: QMediaPlayer | None = None
        self._audio_ready.connect(self._enqueue_audio)
        self._prepared_audio_ready.connect(self._store_prepared_audio)
        self._prepared_audio_failed.connect(self._fail_prepared_audio)
        self._failed.connect(self._log_error)
        self._started.connect(self._run_callback)
        self._finished.connect(self._run_callback)

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
        self._queue_request(
            _TTSRequest(
                text=text,
                tone=tone,
                on_started=on_started,
                on_finished=on_finished,
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
        self._queue_request(_TTSRequest(text=text, tone=tone, prepared_audio=handle))
        return handle

    def speak_prepared(
        self,
        handle: TTSPreparedAudio,
        on_started: TTSCallback | None = None,
        on_finished: TTSCallback | None = None,
    ) -> None:
        if handle.cancelled:
            debug_log("TTS", "预生成句柄已取消，跳过播放", {"text": handle.text, "tone": handle.tone})
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
        with self._request_lock:
            self._pending_requests = [
                request
                for request in self._pending_requests
                if request.prepared_audio is not handle
            ]

        pending_audio: list[
            tuple[Path, TTSCallback | None, TTSCallback | None, TTSPreparedAudio | None]
        ] = []
        for audio_path, on_started, on_finished, prepared_audio in self._pending_audio:
            if prepared_audio is handle:
                self._schedule_audio_cleanup(audio_path)
                continue
            pending_audio.append((audio_path, on_started, on_finished, prepared_audio))
        self._pending_audio = pending_audio

        if handle.audio_path is not None:
            self._schedule_audio_cleanup(handle.audio_path)
            handle.audio_path = None

    def _queue_request(self, request: _TTSRequest) -> None:
        with self._request_lock:
            self._pending_requests.append(request)
            pending_count = len(self._pending_requests)
        debug_log(
            "TTS",
            "请求加入队列",
            {
                "text": request.text,
                "tone": request.tone,
                "prepared": request.prepared_audio is not None,
                "pending_count": pending_count,
            },
        )
        self._start_next_request()

    def _start_next_request(self) -> None:
        with self._request_lock:
            if self._request_running or not self._pending_requests:
                return
            request = self._pending_requests.pop(0)
            self._request_running = True

        debug_log(
            "TTS",
            "开始处理队列请求",
            {
                "text": request.text,
                "tone": request.tone,
                "prepared": request.prepared_audio is not None,
            },
        )
        thread = threading.Thread(
            target=self._request_audio,
            args=(request,),
            daemon=True,
        )
        thread.start()

    def _request_audio(self, tts_request: _TTSRequest) -> None:
        try:
            if tts_request.prepared_audio is not None and tts_request.prepared_audio.cancelled:
                debug_log("TTS", "请求已取消，跳过音频生成", {"text": tts_request.text})
                return

            fail = lambda message: self._fail_audio_request(tts_request, message)
            if not self._ensure_service_available(fail):
                return

            if not self._ensure_character_weights(fail):
                return

            reference = self._select_reference(tts_request.tone)
            payload = {
                "text": tts_request.text,
                "text_lang": _resolve_request_text_lang(
                    tts_request.text,
                    self.settings.text_lang,
                ),
                "ref_audio_path": str(reference.ref_audio_path),
                "prompt_text": reference.ref_text,
                "prompt_lang": reference.ref_lang,
                "text_split_method": "cut1",
                "batch_size": 1,
                "media_type": "wav",
                "streaming_mode": False,
                "top_k": 15,
                "top_p": 1,
                "temperature": 1,
                "repetition_penalty": 1.2,
            }
            debug_log(
                "TTS",
                "发送 GPT-SoVITS 请求",
                {
                    "api_url": self.settings.api_url,
                    "text": tts_request.text,
                    "tone": tts_request.tone,
                    "reference": {
                        "tone": reference.tone,
                        "ref_audio_path": reference.ref_audio_path,
                        "ref_lang": reference.ref_lang,
                    },
                    "payload": payload,
                },
            )
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            http_request = urllib.request.Request(
                url=self.settings.api_url,
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )

            try:
                with urllib.request.urlopen(
                    http_request,
                    timeout=self.settings.timeout_seconds,
                ) as response:
                    audio_data = response.read()
                    debug_log(
                        "TTS",
                        "GPT-SoVITS 请求成功",
                        {
                            "status": getattr(response, "status", None),
                            "audio_bytes": len(audio_data),
                        },
                    )
            except urllib.error.HTTPError as exc:
                error_body = exc.read().decode("utf-8", errors="replace")
                debug_log(
                    "TTS",
                    "GPT-SoVITS HTTP 失败",
                    {
                        "status": exc.code,
                        "error_body": error_body,
                    },
                )
                self._fail_audio_request(tts_request, f"GPT-SoVITS HTTP {exc.code}: {error_body}")
                return
            except urllib.error.URLError as exc:
                debug_log("TTS", "GPT-SoVITS 请求失败", {"reason": str(exc.reason)})
                self._fail_audio_request(
                    tts_request,
                    f"GPT-SoVITS 请求失败，请确认服务已启动并可访问 {self.settings.api_url}：{exc.reason}",
                )
                return
            except TimeoutError:
                debug_log("TTS", "GPT-SoVITS 请求超时")
                self._fail_audio_request(tts_request, "GPT-SoVITS 请求超时。")
                return

            if not audio_data:
                debug_log("TTS", "GPT-SoVITS 返回空音频")
                self._fail_audio_request(tts_request, "GPT-SoVITS 返回了空音频。")
                return

            with tempfile.NamedTemporaryFile(
                prefix="sakura_tts_",
                suffix=".wav",
                delete=False,
            ) as audio_file:
                audio_file.write(audio_data)
                audio_path = audio_file.name
            debug_log("TTS", "临时音频已写入", {"audio_path": audio_path, "bytes": len(audio_data)})
            if tts_request.prepared_audio is None:
                self._audio_ready.emit(audio_path, tts_request.on_started, tts_request.on_finished)
            else:
                self._prepared_audio_ready.emit(tts_request.prepared_audio, audio_path)
        finally:
            with self._request_lock:
                self._request_running = False
            self._start_next_request()

    def _ensure_service_available(
        self,
        fail_callback: Callable[[str], None],
    ) -> bool:
        if self._service_checked:
            debug_log("TTS", "服务探测已完成，跳过重复探测", {"api_url": self.settings.api_url})
            return True

        parsed_url = urlparse(self.settings.api_url)
        host = parsed_url.hostname
        try:
            port = parsed_url.port
        except ValueError as exc:
            debug_log("TTS", "服务地址端口无效", {"api_url": self.settings.api_url, "reason": str(exc)})
            fail_callback(f"GPT-SoVITS 服务地址端口无效：{self.settings.api_url}")
            return False

        if port is None:
            port = 443 if parsed_url.scheme == "https" else 80
        if not host:
            debug_log("TTS", "服务地址无效", {"api_url": self.settings.api_url})
            fail_callback(f"GPT-SoVITS 服务地址无效：{self.settings.api_url}")
            return False

        timeout = min(self.settings.timeout_seconds, 3)
        try:
            debug_log(
                "TTS",
                "探测 GPT-SoVITS 端口",
                {"api_url": self.settings.api_url, "host": host, "port": port},
            )
            with socket.create_connection((host, port), timeout=timeout):
                pass
        except TimeoutError:
            debug_log("TTS", "服务探测超时", {"api_url": self.settings.api_url})
            fail_callback(f"GPT-SoVITS 服务探测超时：{self.settings.api_url}")
            return False
        except OSError as exc:
            debug_log("TTS", "服务不可用", {"reason": str(exc), "api_url": self.settings.api_url})
            fail_callback(f"GPT-SoVITS 服务不可用，请先启动或检查地址 {self.settings.api_url}：{exc}")
            return False

        self._service_checked = True
        debug_log("TTS", "服务探测成功", {"api_url": self.settings.api_url})
        return True

    def _ensure_character_weights(
        self,
        fail_callback: Callable[[str], None],
    ) -> bool:
        if self._weights_ready:
            debug_log("TTS", "角色权重已就绪，跳过切换")
            return True

        for endpoint, path in (
            ("set_gpt_weights", self.settings.gpt_model_path),
            ("set_sovits_weights", self.settings.sovits_model_path),
        ):
            if path is None:
                continue
            debug_log("TTS", "准备切换角色权重", {"endpoint": endpoint, "path": path})
            if not self._request_weight_switch(endpoint, path, fail_callback):
                return False

        self._weights_ready = True
        debug_log("TTS", "角色权重切换完成")
        return True

    def _request_weight_switch(
        self,
        endpoint: str,
        weights_path: Path,
        fail_callback: Callable[[str], None],
    ) -> bool:
        url = _build_tts_endpoint_url(
            self.settings.api_url,
            endpoint,
            {"weights_path": str(weights_path)},
        )
        request = urllib.request.Request(url=url, method="GET")
        try:
            debug_log("TTS", "请求切换权重", {"endpoint": endpoint, "weights_path": weights_path})
            with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                response.read()
                debug_log(
                    "TTS",
                    "权重切换成功",
                    {
                        "endpoint": endpoint,
                        "weights_path": weights_path,
                        "status": getattr(response, "status", None),
                    },
                )
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            debug_log(
                "TTS",
                "权重切换 HTTP 失败",
                {
                    "endpoint": endpoint,
                    "weights_path": weights_path,
                    "status": exc.code,
                    "error_body": error_body,
                },
            )
            fail_callback(
                f"GPT-SoVITS 切换权重失败（{endpoint}, {weights_path}）HTTP {exc.code}: {error_body}"
            )
            return False
        except urllib.error.URLError as exc:
            debug_log(
                "TTS",
                "权重切换请求失败",
                {
                    "endpoint": endpoint,
                    "weights_path": weights_path,
                    "reason": str(exc.reason),
                },
            )
            fail_callback(f"GPT-SoVITS 切换权重失败（{endpoint}, {weights_path}）：{exc.reason}")
            return False
        except TimeoutError:
            debug_log("TTS", "权重切换超时", {"endpoint": endpoint, "weights_path": weights_path})
            fail_callback(f"GPT-SoVITS 切换权重超时（{endpoint}, {weights_path}）。")
            return False
        return True

    def _select_reference(self, tone: str | None) -> ToneReference:
        tone_key = (tone or DEFAULT_TONE).strip() or DEFAULT_TONE
        references = self.settings.tone_references.get(tone_key)
        if not references:
            references = self.settings.tone_references.get(DEFAULT_TONE)
        if not references:
            reference = ToneReference(
                tone=DEFAULT_TONE,
                ref_audio_path=self.settings.ref_audio_path,
                ref_text=self.settings.ref_text,
                ref_lang=self.settings.ref_lang,
            )
            debug_log(
                "TTS",
                "选择默认参考音频",
                {
                    "requested_tone": tone,
                    "ref_audio_path": reference.ref_audio_path,
                    "ref_lang": reference.ref_lang,
                },
            )
            return reference

        index = self._tone_indices.get(tone_key, 0) % len(references)
        self._tone_indices[tone_key] = index + 1
        reference = references[index]
        debug_log(
            "TTS",
            "选择语气参考音频",
            {
                "requested_tone": tone,
                "resolved_tone": tone_key,
                "index": index,
                "count": len(references),
                "ref_audio_path": reference.ref_audio_path,
                "ref_lang": reference.ref_lang,
            },
        )
        return reference

    @Slot(str, object, object)
    def _enqueue_audio(
        self,
        audio_path: str,
        on_started: TTSCallback | None,
        on_finished: TTSCallback | None,
    ) -> None:
        self._pending_audio.append((Path(audio_path), on_started, on_finished, None))
        debug_log(
            "TTS",
            "音频加入播放队列",
            {
                "audio_path": audio_path,
                "pending_audio": len(self._pending_audio),
            },
        )
        if self._current_audio is None:
            self._play_next()

    @Slot(object, str)
    def _store_prepared_audio(self, handle: TTSPreparedAudio, audio_path: str) -> None:
        path = Path(audio_path)
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
        self._log_error(message)
        handle.failed = True
        if handle.cancelled or not handle.play_requested:
            return
        self._started.emit(handle.on_started)
        self._finished.emit(handle.on_finished)
        handle.on_started = None
        handle.on_finished = None

    @Slot(QMediaPlayer.MediaStatus)
    def _handle_media_status(self, status: QMediaPlayer.MediaStatus) -> None:
        debug_log("TTS", "播放器媒体状态变化", {"status": str(status)})
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._finish_current_audio()
            self._play_next()

    @Slot(QMediaPlayer.PlaybackState)
    def _handle_playback_state(self, state: QMediaPlayer.PlaybackState) -> None:
        debug_log("TTS", "播放器播放状态变化", {"state": str(state)})
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._emit_current_started()

    @Slot(QMediaPlayer.Error, str)
    def _handle_player_error(self, _error: QMediaPlayer.Error, error_text: str) -> None:
        debug_log("TTS", "播放器错误", {"error": error_text})
        self._log_error(f"音频播放失败：{error_text}")
        self._finish_current_audio()
        self._play_next()

    @Slot(str)
    def _log_error(self, message: str) -> None:
        print(f"[TTS] {message}")

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
        if request.prepared_audio is None:
            self._fail_request(message, request.on_started, request.on_finished)
            return
        self._prepared_audio_failed.emit(request.prepared_audio, message)

    def _enqueue_prepared_audio(self, handle: TTSPreparedAudio) -> None:
        if handle.cancelled or handle.enqueued or handle.audio_path is None:
            return
        handle.enqueued = True
        self._pending_audio.append(
            (handle.audio_path, handle.on_started, handle.on_finished, handle)
        )
        debug_log(
            "TTS",
            "预生成音频加入播放队列",
            {
                "text": handle.text,
                "tone": handle.tone,
                "audio_path": handle.audio_path,
                "pending_audio": len(self._pending_audio),
            },
        )
        handle.audio_path = None
        if self._current_audio is None:
            self._play_next()

    def _play_next(self) -> None:
        if self._current_audio is not None or not self._pending_audio:
            return
        self._ensure_player()
        (
            self._current_audio,
            self._current_started,
            self._current_finished,
            _prepared_audio,
        ) = self._pending_audio.pop(0)
        self._current_started_emitted = False
        debug_log("TTS", "开始播放音频", {"audio_path": self._current_audio})
        if self._player is None:
            self._fail_audio_playback("播放器初始化失败。")
            return
        self._player.setSource(QUrl.fromLocalFile(str(self._current_audio)))
        self._player.play()

    def _ensure_player(self) -> None:
        if self._player is not None:
            return
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

    def _finish_current_audio(self) -> None:
        if self._finishing_audio:
            return
        audio_path = self._current_audio
        on_finished = self._current_finished
        if audio_path is None:
            self._reset_current_audio_state()
            return
        self._finishing_audio = True
        try:
            debug_log("TTS", "音频播放完成", {"audio_path": audio_path})
            self._emit_current_started()
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
        self._current_started = None
        self._current_finished = None
        self._current_started_emitted = False

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


def _resolve_path(path_text: str, base_dir: Path) -> Path:
    path = Path(path_text.strip().strip('"').strip("'"))
    if path.is_absolute():
        return path
    return base_dir / path


def _load_tone_references(ref_path: Path | None, base_dir: Path) -> dict[str, list[ToneReference]]:
    if ref_path is None or not ref_path.exists():
        return {}

    tone_references: dict[str, list[ToneReference]] = {}
    for raw_line in ref_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        parts = [part.strip() for part in line.split("|")]
        if len(parts) != 4:
            continue

        audio_text, lang, prompt_text, tone = parts
        audio_path = _resolve_path(audio_text, base_dir)
        copied_path = ref_path.parent / "tone_refs" / audio_path.name
        if copied_path.exists():
            audio_path = copied_path

        tone_key = tone or DEFAULT_TONE
        reference = ToneReference(
            tone=tone_key,
            ref_audio_path=audio_path,
            ref_text=prompt_text,
            ref_lang=_normalize_lang(lang),
        )
        tone_references.setdefault(tone_key, []).append(reference)

    return tone_references


def _select_neutral_reference(
    tone_references: dict[str, list[ToneReference]],
) -> ToneReference | None:
    neutral_references = tone_references.get(DEFAULT_TONE)
    if neutral_references:
        return neutral_references[0]
    for references in tone_references.values():
        if references:
            return references[0]
    return None


def _normalize_lang(lang: str) -> str:
    normalized = lang.strip().lower()
    if normalized == "ja":
        return "ja"
    return normalized or "ja"


def _resolve_request_text_lang(text: str, configured_text_lang: str) -> str:
    """英文混入中日韩文本时切到 auto，避免 GPT-SoVITS 按单语 BERT 处理失败。"""
    normalized = configured_text_lang.strip().lower()
    if normalized in _CJK_TEXT_LANGS and _LATIN_LETTER_RE.search(text):
        return "auto_yue" if normalized in {"yue", "all_yue"} else "auto"
    return normalized or "ja"


def _build_tts_endpoint_url(base_url: str, endpoint: str, query: dict[str, str]) -> str:
    parsed_url = urlparse(base_url)
    base_path = parsed_url.path.rsplit("/", 1)[0]
    endpoint_path = f"{base_path}/{endpoint}" if base_path else f"/{endpoint}"
    return urlunparse(
        parsed_url._replace(
            path=endpoint_path,
            query=urlencode(query),
        )
    )
