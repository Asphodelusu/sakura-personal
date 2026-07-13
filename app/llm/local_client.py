"""本地 LLM/VLM 接入与云端路由（M0 · 预留接口）。

Ollama 等本地服务提供 OpenAI 兼容 API，本地端复用 OpenAICompatibleClient。
当前状态：配置与路由门面已接入，**尚未在 8G 级显存等目标环境完成验证**；
生产路径默认始终走云端，仅当用户显式选择「始终本地」时才尝试本地端点。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

from app.config.defaults import (
    DEFAULT_LOCAL_LLM_API_KEY,
    DEFAULT_LOCAL_LLM_BASE_URL,
    DEFAULT_LOCAL_LLM_TIMEOUT_SECONDS,
    DEFAULT_LOCAL_LLM_VISION_ROUTE,
)
from app.core.cancellation import CancelChecker
from app.core.debug_log import debug_log
from app.llm.dual_provider_client import create_cloud_llm_client
from app.llm.api_client import (
    ApiConfigError,
    ApiRequestError,
    ApiSettings,
    ChatCompletionTurn,
    ChatMessage,
    OpenAICompatibleClient,
    messages_contain_image,
)
from app.llm.chat_reply import ChatReply

RouteMode = Literal["cloud", "local", "auto"]
RawTaskKind = Literal["default", "vision", "background"]

# 预留接口状态：untested = 不自动优先本地；verified 后可放开 auto 路由。
LOCAL_LLM_INTERFACE_STATUS: Literal["untested", "verified"] = "untested"


@dataclass(frozen=True)
class LocalLlmSettings:
    """本地 OpenAI 兼容端点（通常为 Ollama）。"""

    enabled: bool = False
    base_url: str = DEFAULT_LOCAL_LLM_BASE_URL
    api_key: str = DEFAULT_LOCAL_LLM_API_KEY
    text_model: str = ""
    vision_model: str = ""
    timeout_seconds: int = DEFAULT_LOCAL_LLM_TIMEOUT_SECONDS
    vision_route: RouteMode = DEFAULT_LOCAL_LLM_VISION_ROUTE
    background_route: RouteMode = "cloud"

    def normalized(self) -> LocalLlmSettings:
        base_url = str(self.base_url or DEFAULT_LOCAL_LLM_BASE_URL).strip().rstrip("/")
        api_key = str(self.api_key or DEFAULT_LOCAL_LLM_API_KEY).strip() or DEFAULT_LOCAL_LLM_API_KEY
        timeout_seconds = max(5, min(600, int(self.timeout_seconds)))
        vision_route = self.vision_route if self.vision_route in {"cloud", "local", "auto"} else "cloud"
        background_route = (
            self.background_route if self.background_route in {"cloud", "local", "auto"} else "cloud"
        )
        return LocalLlmSettings(
            enabled=bool(self.enabled),
            base_url=base_url or DEFAULT_LOCAL_LLM_BASE_URL,
            api_key=api_key,
            text_model=str(self.text_model or "").strip(),
            vision_model=str(self.vision_model or "").strip(),
            timeout_seconds=timeout_seconds,
            vision_route=vision_route,
            background_route=background_route,
        )

    @property
    def active(self) -> bool:
        settings = self.normalized()
        if not settings.enabled:
            return False
        return bool(settings.text_model or settings.vision_model)

    def to_api_settings(self, *, vision: bool = False) -> ApiSettings:
        settings = self.normalized()
        model = settings.vision_model if vision else settings.text_model
        if not model and vision:
            model = settings.text_model
        if not model and not vision:
            model = settings.vision_model
        if not model:
            raise ApiConfigError("本地模型未配置 text_model 或 vision_model。")
        return ApiSettings(
            base_url=settings.base_url,
            api_key=settings.api_key,
            model=model,
            timeout_seconds=settings.timeout_seconds,
        )

    def vision_api_settings(self) -> ApiSettings | None:
        """供 ProactiveObserver 等视觉任务使用的端点；不可用时返回 None。"""
        settings = self.normalized()
        if not settings.enabled:
            return None
        try:
            return settings.to_api_settings(vision=True)
        except ApiConfigError:
            return None


class RoutingLlmClient:
    """云端主对话 + 可选本地后台/视觉的路由门面。"""

    def __init__(
        self,
        cloud_settings: ApiSettings,
        local_settings: LocalLlmSettings | None = None,
    ) -> None:
        self._cloud = create_cloud_llm_client(cloud_settings)
        self._local_settings = (local_settings or LocalLlmSettings()).normalized()
        self._local_client: OpenAICompatibleClient | None = None
        self._refresh_local_client()

    @property
    def settings(self) -> ApiSettings:
        """用户可见对话始终使用云端配置。"""
        return self._cloud.settings

    @property
    def local_settings(self) -> LocalLlmSettings:
        return self._local_settings

    @property
    def cloud_client(self) -> OpenAICompatibleClient:
        return self._cloud

    @property
    def local_client(self) -> OpenAICompatibleClient | None:
        return self._local_client

    @property
    def runtime_context_role(self) -> str:
        return self._cloud.runtime_context_role

    def set_event_emitter(self, emitter) -> None:
        self._cloud.set_event_emitter(emitter)
        if self._local_client is not None:
            self._local_client.set_event_emitter(emitter)

    def update_settings(self, settings: ApiSettings) -> None:
        emitter = getattr(self._cloud, "_event_emit", None)
        self._cloud = create_cloud_llm_client(settings)
        if emitter is not None:
            self._cloud.set_event_emitter(emitter)

    def update_local_settings(self, settings: LocalLlmSettings) -> None:
        self._local_settings = settings.normalized()
        self._refresh_local_client()

    def resolve_dialogue_params(self) -> tuple[float, dict[str, Any]]:
        return self._cloud.resolve_dialogue_params()

    def test_connection(self) -> str:
        return self._cloud.test_connection()

    def test_local_connection(self) -> str:
        client = self._require_local_client(text=True)
        return client.test_connection()

    def list_models(self) -> list[str]:
        return self._cloud.list_models()

    def list_local_models(self) -> list[str]:
        client = self._require_local_client(text=True)
        return client.list_models()

    def resolve_vision_api_settings(self) -> ApiSettings:
        """视觉任务端点；未测试阶段默认云端，仅 route=local 时用本地。"""
        local = self._local_settings.vision_api_settings()
        if local is not None and self._route_allows_local(self._local_settings.vision_route):
            return local
        return self._cloud.resolve_vision_api_settings()

    def chat(
        self,
        system_prompt: str,
        messages: list[ChatMessage],
        reply_tones: list[str] | None = None,
        reply_portraits: list[str] | None = None,
        *,
        cancel_checker: CancelChecker | None = None,
        runtime_context: str = "",
        on_chunk: Callable[[str], None] | None = None,
        **kwargs: Any,
    ) -> ChatReply:
        return self._cloud.chat(
            system_prompt,
            messages,
            reply_tones,
            reply_portraits,
            cancel_checker=cancel_checker,
            runtime_context=runtime_context,
            on_chunk=on_chunk,
            **kwargs,
        )

    def complete_with_tools(
        self,
        system_prompt: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]],
        *,
        cancel_checker=None,
        runtime_context: str = "",
        **kwargs: Any,
    ) -> ChatCompletionTurn:
        return self._cloud.complete_with_tools(
            system_prompt,
            messages,
            tools=tools,
            cancel_checker=cancel_checker,
            runtime_context=runtime_context,
            **kwargs,
        )

    def stream_raw(
        self,
        system_prompt: str,
        messages: list[ChatMessage],
        temperature: float = 0.8,
        *,
        cancel_checker=None,
        runtime_context: str = "",
        **chat_params: Any,
    ):
        return self._cloud.stream_raw(
            system_prompt,
            messages,
            temperature,
            cancel_checker=cancel_checker,
            runtime_context=runtime_context,
            **chat_params,
        )

    def complete_raw(
        self,
        system_prompt: str,
        messages: list[ChatMessage],
        temperature: float = 0.8,
        *,
        cancel_checker=None,
        runtime_context: str = "",
        task: RawTaskKind = "default",
        **chat_params: Any,
    ) -> str:
        client, backend = self._resolve_raw_client(messages, task=task)
        debug_log(
            "LocalLLM",
            "complete_raw 路由",
            {
                "backend": backend,
                "task": task,
                "has_image": messages_contain_image(messages),
                "model": client.settings.model,
            },
        )
        try:
            return client.complete_raw(
                system_prompt,
                messages,
                temperature,
                cancel_checker=cancel_checker,
                runtime_context=runtime_context,
                task=task,
                **chat_params,
            )
        except ApiRequestError as exc:
            if backend == "local":
                debug_log(
                    "LocalLLM",
                    "本地请求失败，回退云端",
                    {"error": str(exc), "task": task},
                )
                return self._cloud.complete_raw(
                    system_prompt,
                    messages,
                    temperature,
                    cancel_checker=cancel_checker,
                    runtime_context=runtime_context,
                    task=task,
                    **chat_params,
                )
            raise

    def _resolve_raw_client(
        self,
        messages: list[ChatMessage],
        *,
        task: RawTaskKind,
    ) -> tuple[OpenAICompatibleClient, str]:
        if task == "vision" or (task == "default" and messages_contain_image(messages)):
            route = self._local_settings.vision_route
            prefer_vision = True
        elif task == "background":
            route = self._local_settings.background_route
            prefer_vision = False
        else:
            return self._cloud, "cloud"

        if not self._route_allows_local(route):
            return self._cloud, "cloud"

        client = self._local_client_for(prefer_vision=prefer_vision)
        if client is None:
            return self._cloud, "cloud"
        return client, "local"

    def _route_allows_local(self, route: RouteMode) -> bool:
        if route == "cloud":
            return False
        if route == "auto":
            # 接口预留：auto 在 verified 之前等同 cloud，避免未测硬件被静默切到本地。
            if LOCAL_LLM_INTERFACE_STATUS != "verified":
                return False
            return self._local_settings.active
        if route == "local":
            return self._local_settings.active
        return False

    def _local_client_for(self, *, prefer_vision: bool) -> OpenAICompatibleClient | None:
        if not self._local_settings.active:
            return None
        try:
            settings = self._local_settings.to_api_settings(vision=prefer_vision)
        except ApiConfigError:
            return None
        if (
            self._local_client is not None
            and self._local_client.settings.base_url == settings.base_url
            and self._local_client.settings.model == settings.model
            and self._local_client.settings.api_key == settings.api_key
            and self._local_client.settings.timeout_seconds == settings.timeout_seconds
        ):
            return self._local_client
        client = OpenAICompatibleClient(settings)
        emitter = getattr(self._cloud, "_event_emit", None)
        if emitter is not None:
            client.set_event_emitter(emitter)
        return client

    def _require_local_client(self, *, text: bool) -> OpenAICompatibleClient:
        client = self._local_client_for(prefer_vision=not text)
        if client is None:
            raise ApiConfigError("本地模型未启用或未配置可用模型。")
        return client

    def _refresh_local_client(self) -> None:
        self._local_client = None
        if not self._local_settings.active:
            return
        try:
            self._local_client = OpenAICompatibleClient(
                self._local_settings.to_api_settings(vision=False)
            )
        except ApiConfigError:
            self._local_client = None


def create_routing_llm_client(
    cloud_settings: ApiSettings,
    local_settings: LocalLlmSettings | None = None,
) -> RoutingLlmClient:
    return RoutingLlmClient(cloud_settings, local_settings)


def is_routing_llm_client(client: object) -> bool:
    return isinstance(client, RoutingLlmClient)
