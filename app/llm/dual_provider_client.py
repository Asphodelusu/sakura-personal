from __future__ import annotations

from typing import Any, Callable

from app.core.cancellation import CancelChecker
from app.llm.api_client import (
    ApiSettings,
    ChatCompletionTurn,
    ChatMessage,
    OpenAICompatibleClient,
    api_settings_for_text,
    api_settings_for_vision,
    api_settings_uses_dual_endpoint,
    messages_contain_image,
    prepare_messages_for_chat_api,
)
from app.llm.chat_reply import ChatReply


class DualProviderLlmClient:
    """文本与视觉使用不同 API 端点时的路由门面（如 DeepSeek 文本 + 智谱视觉）。"""

    def __init__(self, settings: ApiSettings) -> None:
        self._settings = settings
        self._vision = OpenAICompatibleClient(api_settings_for_vision(settings))
        self._text = OpenAICompatibleClient(api_settings_for_text(settings))

    @property
    def settings(self) -> ApiSettings:
        return self._settings

    @property
    def vision_client(self) -> OpenAICompatibleClient:
        return self._vision

    @property
    def text_client(self) -> OpenAICompatibleClient:
        return self._text

    @property
    def runtime_context_role(self) -> str:
        return self._text.runtime_context_role

    def set_event_emitter(self, emitter: Callable[[str, dict[str, Any]], None] | None) -> None:
        self._vision.set_event_emitter(emitter)
        self._text.set_event_emitter(emitter)

    def update_settings(self, settings: ApiSettings) -> None:
        self._settings = settings
        self._vision.update_settings(api_settings_for_vision(settings))
        self._text.update_settings(api_settings_for_text(settings))

    def resolve_dialogue_params(self) -> tuple[float, dict[str, Any]]:
        return self._text.resolve_dialogue_params()

    def resolve_vision_api_settings(self) -> ApiSettings:
        return self._vision.resolve_vision_api_settings()

    def test_connection(self) -> str:
        vision_msg = self._vision.test_connection()
        text_msg = self._text.test_connection()
        return f"视觉端点：{vision_msg}\n文本端点：{text_msg}"

    def list_models(self) -> list[str]:
        return self._vision.list_models()

    def list_text_models(self) -> list[str]:
        return self._text.list_models()

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
    ) -> ChatReply:
        client = self._pick_client(messages)
        prepared_messages = prepare_messages_for_chat_api(
            messages,
            text_only=client is self._text,
        )
        reply = client.chat(
            system_prompt,
            prepared_messages,
            reply_tones,
            reply_portraits,
            cancel_checker=cancel_checker,
            runtime_context=runtime_context,
            on_chunk=on_chunk,
        )
        self._mirror_runtime_context_role(client)
        return reply

    def complete_raw(
        self,
        system_prompt: str,
        messages: list[ChatMessage],
        temperature: float = 0.8,
        *,
        cancel_checker: CancelChecker | None = None,
        runtime_context: str = "",
        task: str = "default",
        **chat_params: Any,
    ) -> str:
        client = self._pick_client(messages, task=task)
        prepared_messages = prepare_messages_for_chat_api(
            messages,
            text_only=client is self._text,
        )
        result = client.complete_raw(
            system_prompt,
            prepared_messages,
            temperature,
            cancel_checker=cancel_checker,
            runtime_context=runtime_context,
            **chat_params,
        )
        self._mirror_runtime_context_role(client)
        return result

    def stream_raw(
        self,
        system_prompt: str,
        messages: list[ChatMessage],
        temperature: float = 0.8,
        *,
        cancel_checker: CancelChecker | None = None,
        runtime_context: str = "",
        **chat_params: Any,
    ):
        client = self._pick_client(messages)
        prepared_messages = prepare_messages_for_chat_api(
            messages,
            text_only=client is self._text,
        )
        for chunk in client.stream_raw(
            system_prompt,
            prepared_messages,
            temperature,
            cancel_checker=cancel_checker,
            runtime_context=runtime_context,
            **chat_params,
        ):
            yield chunk
        self._mirror_runtime_context_role(client)

    def complete_with_tools(
        self,
        system_prompt: str,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = "auto",
        temperature: float = 0.8,
        structured_response: bool = False,
        runtime_context: str = "",
        cancel_checker: CancelChecker | None = None,
        **chat_params: Any,
    ) -> ChatCompletionTurn:
        client = self._pick_client(messages)
        prepared_messages = prepare_messages_for_chat_api(
            messages,
            text_only=client is self._text,
        )
        turn = client.complete_with_tools(
            system_prompt,
            prepared_messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            structured_response=structured_response,
            runtime_context=runtime_context,
            cancel_checker=cancel_checker,
            **chat_params,
        )
        self._mirror_runtime_context_role(client)
        return turn

    def _pick_client(self, messages: list[ChatMessage], *, task: str = "default") -> OpenAICompatibleClient:
        # 后台 JSON 任务走视觉端点（GLM），利用其原生 json_object + thinking 可控
        if task == "background":
            return self._vision
        if messages_contain_image(messages):
            return self._vision
        return self._text

    def _mirror_runtime_context_role(self, source: OpenAICompatibleClient) -> None:
        role = source.runtime_context_role
        if self._vision.runtime_context_role != role:
            self._vision._runtime_context_role = role  # noqa: SLF001
        if self._text.runtime_context_role != role:
            self._text._runtime_context_role = role  # noqa: SLF001


def create_cloud_llm_client(settings: ApiSettings) -> OpenAICompatibleClient | DualProviderLlmClient:
    if api_settings_uses_dual_endpoint(settings):
        return DualProviderLlmClient(settings)
    return OpenAICompatibleClient(settings)


def is_dual_provider_client(client: object) -> bool:
    return isinstance(client, DualProviderLlmClient)
