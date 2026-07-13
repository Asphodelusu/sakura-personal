from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

import httpx

from app.core.cancellation import CancelChecker, cancellable_sleep, check_cancelled
from app.core.debug_log import debug_log, summarize_messages
from app.llm.chat_reply import ChatReply, parse_chat_reply, sanitize_reply_tones
from app.llm.prompt_templates import build_segmented_reply_instruction


MAX_API_RETRY_ATTEMPTS = 3
API_RETRY_DELAY_SECONDS = 1.0
API_RETRY_JITTER = 0.25
STRUCTURED_JSON_RESPONSE_FORMAT = {"type": "json_object"}
ChatMessage = dict[str, Any]
SUPPORTED_CHAT_COMPLETION_PARAMS = {
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "presence_penalty",
    "frequency_penalty",
    "response_format",
    "stream",
    "tools",
    "tool_choice",
    "thinking",
}


class ApiConfigError(RuntimeError):
    """API 配置缺失或格式错误。"""


class ApiRequestError(RuntimeError):
    """API 请求失败。"""


@dataclass(frozen=True)
class ApiSettings:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: int = 60
    # 图文分流：model 为视觉/默认模型；启用且填写 text_model 时，无图请求走 text_model。
    text_model: str = ""
    model_split_enabled: bool = False
    # 双端点：文本与视觉使用不同的 base_url / api_key（如 DeepSeek 文本 + 智谱视觉）。
    dual_endpoint_enabled: bool = False
    text_base_url: str = ""
    text_api_key: str = ""
    # 角色对话生成参数；None 表示沿用内置默认/不发送该参数，保持历史行为。
    temperature: float | None = None  # None → 角色对话用内置默认 0.8
    top_p: float | None = None  # None → 不发送 top_p
    max_tokens: int | None = None  # None → 不发送 max_tokens（不截断输出）
    frequency_penalty: float | None = None  # None → 不发送，建议 0.3-0.5 防复读
    presence_penalty: float | None = None  # None → 不发送，建议 0.2-0.4 增加多样性


DEFAULT_TEXT_PROVIDER_BASE_URL = "https://api.deepseek.com"


def normalize_provider_base_url(base_url: str) -> str:
    """修正常见填错的提供商地址（如 DeepSeek 开放平台网页）。"""
    normalized = base_url.strip().rstrip("/")
    lowered = normalized.lower()
    if "platform.deepseek.com" in lowered or lowered in {
        "https://www.deepseek.com",
        "http://www.deepseek.com",
        "https://deepseek.com",
        "http://deepseek.com",
    }:
        return DEFAULT_TEXT_PROVIDER_BASE_URL
    return normalized


def _looks_like_html_response(body: str) -> bool:
    stripped = body.lstrip()
    return stripped.startswith("<!") or stripped.lower().startswith("<html")


def _truncate_diagnostic(body: str, limit: int = 280) -> str:
    text = " ".join(body.split())
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _friendly_non_json_api_error(base_url: str, body: str) -> str:
    if _looks_like_html_response(body):
        if "platform.deepseek.com" in body.lower() or "platform.deepseek.com" in base_url.lower():
            return (
                "文本 Base URL 填成了 DeepSeek 开放平台网页地址。"
                "请改为 https://api.deepseek.com（不是 platform.deepseek.com）。"
            )
        if "request blocked" in body.lower():
            return (
                "请求被拦截（Request Blocked）。请确认 Base URL 为 API 地址"
                "（DeepSeek：https://api.deepseek.com），并检查代理/防火墙。"
            )
        return (
            "API 返回了网页 HTML 而非 JSON，请确认 Base URL 指向 API 端点。"
            "DeepSeek 应填 https://api.deepseek.com 。"
        )
    return f"API 返回格式无法解析：{_truncate_diagnostic(body)}"


def api_settings_uses_dual_endpoint(settings: ApiSettings) -> bool:
    """双端点模式已移除；槽位分流由 api_profiles + model_slots 承担。"""
    _ = settings
    return False


def api_settings_for_vision(settings: ApiSettings) -> ApiSettings:
    """单端点分流或双端点模式下的视觉侧配置。"""
    return replace(
        settings,
        model=settings.model.strip(),
        text_model="",
        model_split_enabled=False,
        dual_endpoint_enabled=False,
        text_base_url="",
        text_api_key="",
    )


def api_settings_for_text(settings: ApiSettings) -> ApiSettings:
    """单端点分流或双端点模式下的文本侧配置。"""
    if api_settings_uses_dual_endpoint(settings):
        return replace(
            settings,
            base_url=normalize_provider_base_url(settings.text_base_url).strip().rstrip("/"),
            api_key=settings.text_api_key.strip(),
            model=settings.text_model.strip(),
            text_model="",
            model_split_enabled=False,
            dual_endpoint_enabled=False,
            text_base_url="",
            text_api_key="",
        )
    return replace(
        settings,
        model=settings.text_model.strip() or settings.model.strip(),
        text_model="",
        model_split_enabled=False,
        dual_endpoint_enabled=False,
        text_base_url="",
        text_api_key="",
    )


@dataclass(frozen=True)
class NativeToolCall:
    """OpenAI 原生 tool_call，保留 id 以便后续 tool role 回填。"""

    id: str
    name: str
    arguments: dict[str, Any]
    arguments_json: str = "{}"


@dataclass(frozen=True)
class ChatCompletionTurn:
    """一次 Chat Completions 返回的 assistant 消息。"""

    content: str
    tool_calls: list[NativeToolCall]
    message: dict[str, Any]
    runtime_context_role: str = "system"


class OpenAICompatibleClient:
    def __init__(self, settings: ApiSettings) -> None:
        self.settings = settings
        self._unsupported_chat_params: set[str] = set()
        self._runtime_context_role = "system"
        # 可选事件发射器（由宿主注入），用于派发 llm.request.* 插件事件。
        self._event_emit: Callable[[str, dict[str, Any] | None], None] | None = None
        self._http: httpx.Client | None = None

    def _http_client(self) -> httpx.Client:
        """获取或创建可复用的 httpx.Client，连接池复用 TCP 连接。"""
        if self._http is None:
            self._http = httpx.Client(
                base_url=_normalize_openai_base_url(self.settings.base_url),
                timeout=httpx.Timeout(self.settings.timeout_seconds),
                headers={"Authorization": f"Bearer {self.settings.api_key}"},
                limits=httpx.Limits(max_keepalive_connections=4, max_connections=20),
            )
        return self._http

    def _close_http(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def set_event_emitter(
        self,
        emitter: Callable[[str, dict[str, Any] | None], None] | None,
    ) -> None:
        """注入插件事件发射器；传 None 关闭。"""
        self._event_emit = emitter

    def _emit_llm_event(self, event_name: str, payload: dict[str, Any] | None = None) -> None:
        """安全派发 LLM 请求事件，发射器异常不影响请求本身。"""
        emitter = self._event_emit
        if emitter is None:
            return
        try:
            emitter(event_name, payload)
        except Exception:  # noqa: BLE001 — 事件派发不得影响 LLM 请求
            pass

    def update_settings(self, settings: ApiSettings) -> None:
        """运行时更新 API 配置，供设置界面保存后立即生效。"""
        self.settings = settings
        self._unsupported_chat_params.clear()
        self._runtime_context_role = "system"
        self._close_http()
    @property
    def runtime_context_role(self) -> str:
        return self._runtime_context_role


    def resolve_dialogue_params(self) -> tuple[float, dict[str, Any]]:
        """返回角色对话用的生成参数：温度 + 额外参数（top_p/max_tokens）。

        仅供角色对话入口（chat() 与 Agent 主工具循环）调用；记忆抽取、视觉摘要、
        JSON 修复等内部功能调用必须保留各自硬编码的低温度，不得使用本方法，
        否则会被用户配置污染。未配置的字段回退到内置默认（温度 0.8）或直接不发送。
        """
        temperature = self.settings.temperature if self.settings.temperature is not None else 0.8
        extra: dict[str, Any] = {}
        if self.settings.top_p is not None:
            extra["top_p"] = self.settings.top_p
        if self.settings.max_tokens is not None:
            extra["max_tokens"] = self.settings.max_tokens
        if self.settings.frequency_penalty is not None:
            extra["frequency_penalty"] = self.settings.frequency_penalty
        if self.settings.presence_penalty is not None:
            extra["presence_penalty"] = self.settings.presence_penalty
        return temperature, extra

    def resolve_vision_api_settings(self) -> ApiSettings:
        """返回视觉任务用的 API 配置（图文分流时 model 即为视觉模型）。"""
        return replace(self.settings, model=self.settings.model.strip())

    def _resolve_request_model(self, messages: list[ChatMessage]) -> str:
        return resolve_chat_model(self.settings, messages)

    def test_connection(self) -> str:
        """发送一次最小聊天请求，验证 Base URL、API Key 和模型是否可用。"""
        self._ensure_chat_config("缺少 API_KEY。请在设置中填写 API Key。")

        # 连通性检测只需验证 Base URL / API Key / 模型可用，不发送 temperature：
        # 部分模型（如 o1/o3/gpt-5 等推理模型）只接受默认温度，显式传值会直接报错。
        probe_model = (
            self.settings.text_model.strip()
            if self.settings.model_split_enabled and self.settings.text_model.strip()
            else self.settings.model
        )
        payload = {
            "model": probe_model,
            "messages": [
                {
                    "role": "user",
                    "content": "Reply with only OK.",
                },
            ],
            "max_tokens": 8,
        }
        data = self._post_chat_completions_with_compatibility_fallbacks(payload)

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ApiRequestError(
                f"API 返回格式无法解析：{_truncate_diagnostic(json.dumps(data, ensure_ascii=False))}"
            ) from exc

        return str(content).strip() or "OK"

    def list_models(self) -> list[str]:
        """读取 OpenAI 兼容 /models 接口，返回可选择的模型 id 列表。"""
        self._ensure_model_list_config()
        base_url = normalize_provider_base_url(self.settings.base_url)
        debug_log(
            "API",
            "准备检测模型列表",
            {
                "base_url": _normalize_openai_base_url(base_url),
                "configured_base_url": self.settings.base_url,
                "timeout_seconds": self.settings.timeout_seconds,
            },
        )
        response_body = self._send_http_with_retries("GET", "/models")
        if _looks_like_html_response(response_body):
            raise ApiRequestError(_friendly_non_json_api_error(base_url, response_body))
        try:
            data: dict[str, Any] = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise ApiRequestError(_friendly_non_json_api_error(base_url, response_body)) from exc

        return _parse_model_ids(data)

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
        """角色聊天回复，支持流式回调 on_chunk。"""
        segmented_reply_instruction = _build_segmented_reply_instruction(reply_tones, reply_portraits)
        temperature, extra_params = self.resolve_dialogue_params()
        if on_chunk is not None:
            content = self._stream_accumulate(
                f"{system_prompt.strip()}\n\n{segmented_reply_instruction}",
                messages,
                temperature=temperature,
                response_format=STRUCTURED_JSON_RESPONSE_FORMAT,
                cancel_checker=cancel_checker,
                runtime_context=runtime_context,
                on_chunk=on_chunk,
                **extra_params,
            )
        else:
            content = self.complete_raw(
                f"{system_prompt.strip()}\n\n{segmented_reply_instruction}",
                messages,
                temperature=temperature,
                response_format=STRUCTURED_JSON_RESPONSE_FORMAT,
                cancel_checker=cancel_checker,
                runtime_context=runtime_context,
                **extra_params,
            )
        check_cancelled(cancel_checker)

        reply = sanitize_reply_tones(parse_chat_reply(content), reply_tones)
        debug_log(
            "API",
            "聊天回复解析完成",
            {
                "segments": len(reply.segments),
                "tone": reply.tone,
                "portraits": [segment.portrait for segment in reply.segments],
                "reply": reply.text,
            },
        )
        return reply

    def _stream_accumulate(
        self,
        system_prompt: str,
        messages: list[ChatMessage],
        temperature: float = 0.8,
        *,
        cancel_checker: CancelChecker | None = None,
        runtime_context: str = "",
        on_chunk: Callable[[str], None],
        **chat_params: Any,
    ) -> str:
        """流式获取并累积完整响应，同时通过 on_chunk 回调每个文本块。"""
        chunks: list[str] = []
        for chunk in self.stream_raw(
            system_prompt,
            messages,
            temperature=temperature,
            cancel_checker=cancel_checker,
            runtime_context=runtime_context,
            **chat_params,
        ):
            chunks.append(chunk)
            on_chunk(chunk)
        return "".join(chunks)

    def complete_raw(
        self,
        system_prompt: str,
        messages: list[ChatMessage],
        temperature: float = 0.8,
        *,
        cancel_checker: CancelChecker | None = None,
        runtime_context: str = "",
        task: str | None = None,
        **chat_params: Any,
    ) -> str:
        """返回模型原始文本，供 Agent Runtime 解析工具调用 JSON。

        task 仅由 RoutingLlmClient / DualProviderLlmClient 消费，底层直连时忽略。
        """
        _ = task
        self._ensure_chat_config("缺少 API Key。请在 data/config/api.yaml 中配置 llm.api_key。")
        check_cancelled(cancel_checker)
        runtime_context_role = self._runtime_context_role
        request_model = self._resolve_request_model(messages)
        payload = _build_chat_completion_payload(
            model=request_model,
            system_prompt=system_prompt,
            messages=_messages_with_runtime_context(
                messages, runtime_context, runtime_context_role
            ),
            temperature=temperature,
            chat_params=chat_params,
        )
        debug_log(
            "API",
            "准备发送聊天补全请求",
            {
                "base_url": _normalize_openai_base_url(self.settings.base_url),
                "configured_base_url": self.settings.base_url,
                "model": request_model,
                "model_split_enabled": self.settings.model_split_enabled,
                "timeout_seconds": self.settings.timeout_seconds,
                "temperature": temperature,
                "message_count": len(payload["messages"]),
                "has_image": messages_contain_image(payload["messages"]),
                "messages": summarize_messages(payload["messages"]),
                "chat_params": _filter_supported_chat_params(chat_params),
            },
        )
        try:
            data = self._post_chat_completions_with_compatibility_fallbacks(
                payload,
                cancel_checker=cancel_checker,
            )
        except ApiRequestError as exc:
            if (
                runtime_context.strip()
                and runtime_context_role == "system"
                and _is_runtime_context_role_unsupported_error(exc)
            ):
                self._runtime_context_role = "user"
                payload = _build_chat_completion_payload(
                    model=request_model,
                    system_prompt=system_prompt,
                    messages=_messages_with_runtime_context(messages, runtime_context, "user"),
                    temperature=temperature,
                    chat_params=chat_params,
                )
                debug_log(
                    "API",
                    "端点不支持尾部 system 上下文，已回退为 user 上下文",
                    {"error": str(exc)},
                )
                data = self._post_chat_completions_with_compatibility_fallbacks(
                    payload, cancel_checker=cancel_checker
                )
            else:
                raise
        check_cancelled(cancel_checker)

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ApiRequestError(f"API 返回格式无法解析：{json.dumps(data, ensure_ascii=False)}") from exc

        result = str(content).strip() if content else ""
        debug_log("API", "模型原始文本返回", {"content": result})
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
        """流式聊天补全，逐块 yield 文本内容。调用方累积后自行解析。"""
        self._ensure_chat_config("缺少 API Key。请在 data/config/api.yaml 中配置 llm.api_key。")
        check_cancelled(cancel_checker)
        request_model = self._resolve_request_model(messages)
        payload = _build_chat_completion_payload(
            model=request_model,
            system_prompt=system_prompt,
            messages=_messages_with_runtime_context(
                messages, runtime_context, self._runtime_context_role
            ),
            temperature=temperature,
            chat_params={**chat_params, "stream": True},
        )
        debug_log(
            "API",
            "准备发送流式聊天补全请求",
            {
                "base_url": _normalize_openai_base_url(self.settings.base_url),
                "model": request_model,
                "model_split_enabled": self.settings.model_split_enabled,
                "temperature": temperature,
                "message_count": len(payload["messages"]),
            },
        )
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        model_name = payload.get("model")
        self._emit_llm_event("llm.request.started", {"model": model_name})
        try:
            client = self._http_client()
            with client.stream(
                "POST",
                "/chat/completions",
                content=body,
                headers={"Content-Type": "application/json"},
                timeout=httpx.Timeout(
                    self.settings.timeout_seconds,
                    read=self.settings.timeout_seconds,
                ),
            ) as response:
                if response.status_code >= 400:
                    error_body = response.read().decode("utf-8", errors="replace")
                    raise ApiRequestError(
                        _format_api_http_error(
                            response.status_code, error_body, str(response.url)
                        )
                    )
                for line in response.iter_lines():
                    check_cancelled(cancel_checker)
                    line = line.strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    try:
                        delta = chunk["choices"][0]["delta"]
                    except (KeyError, IndexError, TypeError):
                        continue
                    delta_content = delta.get("content")
                    if isinstance(delta_content, str) and delta_content:
                        yield delta_content
                    reasoning = delta.get("reasoning_content")
                    if isinstance(reasoning, str) and reasoning:
                        yield reasoning
        except (httpx.HTTPStatusError, httpx.ConnectError,
                httpx.NetworkError, httpx.RemoteProtocolError,
                httpx.ReadTimeout) as exc:
            self._emit_llm_event(
                "llm.request.failed",
                {"model": model_name, "error": str(exc)},
            )
            raise ApiRequestError(f"API 流式请求失败：{exc}") from exc
        except Exception:
            self._emit_llm_event(
                "llm.request.failed",
                {"model": model_name, "error": "stream_error"},
            )
            raise
        self._emit_llm_event("llm.request.finished", {"model": model_name})

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
        """调用 OpenAI 原生 tools/tool_calls 协议并返回 assistant 消息。"""
        self._ensure_chat_config("缺少 API Key。请在 data/config/api.yaml 中配置 llm.api_key。")
        check_cancelled(cancel_checker)
        messages = sanitize_tool_conversation_messages(messages)

        if tools:
            chat_params["tools"] = tools
            chat_params["tool_choice"] = tool_choice
        if structured_response and "response_format" not in chat_params:
            chat_params["response_format"] = STRUCTURED_JSON_RESPONSE_FORMAT
        runtime_context_role = self._runtime_context_role
        request_messages = _messages_with_runtime_context(
            messages, runtime_context, runtime_context_role
        )
        request_model = self._resolve_request_model(request_messages)
        payload = _build_chat_completion_payload(
            model=request_model,
            system_prompt=system_prompt,
            messages=request_messages,
            temperature=temperature,
            chat_params=chat_params,
        )
        debug_log(
            "API",
            "准备发送原生工具聊天补全请求",
            {
                "base_url": _normalize_openai_base_url(self.settings.base_url),
                "configured_base_url": self.settings.base_url,
                "model": request_model,
                "model_split_enabled": self.settings.model_split_enabled,
                "timeout_seconds": self.settings.timeout_seconds,
                "temperature": temperature,
                "message_count": len(payload["messages"]),
                "tool_count": len(tools or []),
                "has_image": messages_contain_image(payload["messages"]),
                "messages": summarize_messages(payload["messages"]),
                "chat_params": _filter_supported_chat_params(chat_params),
            },
        )
        try:
            data = self._post_chat_completions_with_compatibility_fallbacks(
                payload,
                cancel_checker=cancel_checker,
            )
        except ApiRequestError as exc:
            if (
                runtime_context.strip()
                and runtime_context_role == "system"
                and _is_runtime_context_role_unsupported_error(exc)
            ):
                self._runtime_context_role = "user"
                runtime_context_role = "user"
                payload = _build_chat_completion_payload(
                    model=request_model,
                    system_prompt=system_prompt,
                    messages=_messages_with_runtime_context(messages, runtime_context, "user"),
                    temperature=temperature,
                    chat_params=chat_params,
                )
                debug_log(
                    "API",
                    "端点不支持尾部 system 上下文，已回退为 user 上下文",
                    {"error": str(exc)},
                )
                data = self._post_chat_completions_with_compatibility_fallbacks(
                    payload, cancel_checker=cancel_checker
                )
            else:
                raise
        check_cancelled(cancel_checker)

        choice_diagnostics = _extract_choice_diagnostics(data)
        try:
            raw_message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ApiRequestError(f"API 返回格式无法解析：{json.dumps(data, ensure_ascii=False)}") from exc
        if not isinstance(raw_message, dict):
            raise ApiRequestError(f"API 返回 message 格式无法解析：{json.dumps(data, ensure_ascii=False)}")

        content = raw_message.get("content")
        tool_calls = _parse_native_tool_calls(raw_message.get("tool_calls"))
        if not tool_calls:
            tool_calls = _parse_pseudo_tool_calls_from_content(content)
            # 如果从 content 中解析出了 XML 格式的 tool_call，从可见文本中移除
            if tool_calls and isinstance(content, str):
                content = _strip_xml_tool_calls(content)
        normalized_message = _normalize_assistant_message(raw_message, content, tool_calls)
        response_log: dict[str, Any] = {
            "content": str(content or "").strip(),
            "tool_calls": [
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in tool_calls
            ],
            **choice_diagnostics,
        }
        debug_log("API", "原生工具模型返回", response_log)
        if not str(content or "").strip() and not tool_calls:
            debug_log(
                "API",
                "空 content 且无 tool_calls",
                {
                    **choice_diagnostics,
                    "tool_count": len(tools or []),
                    "structured_response": structured_response,
                },
            )
        return ChatCompletionTurn(
            content=str(content or "").strip(),
            tool_calls=tool_calls,
            message=normalized_message,
            runtime_context_role=runtime_context_role,
        )

    def _post_chat_completions_with_compatibility_fallbacks(
        self,
        payload: dict[str, Any],
        *,
        cancel_checker: CancelChecker | None = None,
    ) -> dict[str, Any]:
        fallback_payload = dict(payload)
        for param in self._unsupported_chat_params:
            fallback_payload.pop(param, None)
        while True:
            check_cancelled(cancel_checker)
            try:
                return self._post_chat_completions(
                    fallback_payload,
                    cancel_checker=cancel_checker,
                )
            except ApiRequestError as exc:
                if "response_format" in fallback_payload and _is_response_format_unsupported_error(exc):
                    self._unsupported_chat_params.add("response_format")
                    fallback_payload.pop("response_format", None)
                    debug_log(
                        "API",
                        "结构化 response_format 不受支持，已回退普通请求",
                        {"error": str(exc)},
                    )
                    continue
                if "temperature" in fallback_payload and _is_temperature_unsupported_error(exc):
                    self._unsupported_chat_params.add("temperature")
                    fallback_payload.pop("temperature", None)
                    debug_log(
                        "API",
                        "模型不支持自定义 temperature，已回退默认温度",
                        {"error": str(exc)},
                    )
                    continue
                raise

    def _ensure_chat_config(self, api_key_message: str) -> None:
        if not self.settings.api_key:
            raise ApiConfigError(api_key_message)
        if not self.settings.base_url:
            raise ApiConfigError("缺少 BASE_URL。")
        if not self.settings.model:
            raise ApiConfigError("缺少 MODEL。")

    def _ensure_model_list_config(self) -> None:
        if not self.settings.api_key:
            raise ApiConfigError("缺少 API_KEY。请在设置中填写 API Key。")
        if not self.settings.base_url:
            raise ApiConfigError("缺少 BASE_URL。")

    def _post_chat_completions(
        self,
        payload: dict[str, Any],
        *,
        cancel_checker: CancelChecker | None = None,
    ) -> dict[str, Any]:
        """调用 OpenAI 兼容的 chat/completions 接口并返回 JSON 数据。"""
        check_cancelled(cancel_checker)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        debug_log(
            "API",
            "HTTP 请求体已构建",
            {
                "base_url": _normalize_openai_base_url(self.settings.base_url),
                "configured_base_url": self.settings.base_url,
                "bytes": len(body),
                "payload": payload,
            },
        )
        model_name = payload.get("model")
        self._emit_llm_event("llm.request.started", {"model": model_name})
        try:
            response_body = self._send_http_with_retries(
                "POST", "/chat/completions",
                content=body,
                headers={"Content-Type": "application/json"},
                cancel_checker=cancel_checker,
            )
            check_cancelled(cancel_checker)
            try:
                data: dict[str, Any] = json.loads(response_body)
            except json.JSONDecodeError as exc:
                raise ApiRequestError(
                    _friendly_non_json_api_error(self.settings.base_url, response_body)
                ) from exc
        except Exception as exc:  # noqa: BLE001 — 仅用于派发失败事件，随后原样抛出
            self._emit_llm_event(
                "llm.request.failed",
                {"model": model_name, "error": str(exc)},
            )
            raise

        self._emit_llm_event("llm.request.finished", {"model": model_name})
        return data

    def _send_http_with_retries(
        self,
        method: str,
        path: str,
        *,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        cancel_checker: CancelChecker | None = None,
    ) -> str:
        """使用 httpx 连接池发送 HTTP 请求，支持自动重试。"""
        last_error: BaseException | None = None
        for attempt in range(1, MAX_API_RETRY_ATTEMPTS + 1):
            check_cancelled(cancel_checker)
            started_at = time.perf_counter()
            try:
                client = self._http_client()
                request = client.build_request(
                    method=method,
                    url=path,
                    content=content,
                    headers=headers,
                )
                response = client.send(request)
                response_body = response.text
                debug_log(
                    "API",
                    "HTTP 请求成功",
                    {
                        "attempt": attempt,
                        "status": response.status_code,
                        "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
                        "response_body": response_body,
                    },
                )
                return response_body
            except httpx.HTTPStatusError as exc:
                error_body = exc.response.text
                status_code = exc.response.status_code
                url = str(exc.request.url) if exc.request is not None else path
                debug_log(
                    "API",
                    "HTTP 请求失败",
                    {
                        "attempt": attempt,
                        "status": status_code,
                        "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
                        "error_body": error_body,
                    },
                )
                if status_code not in {429, 500, 502, 503, 504} or attempt == MAX_API_RETRY_ATTEMPTS:
                    raise ApiRequestError(_format_api_http_error(status_code, error_body, url)) from exc
                last_error = exc
            except (httpx.ConnectError, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                debug_log(
                    "API",
                    "连接/网络错误",
                    {
                        "attempt": attempt,
                        "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
                        "error": str(exc),
                    },
                )
                if attempt == MAX_API_RETRY_ATTEMPTS:
                    raise ApiRequestError(f"API 请求失败：{exc}") from exc
                last_error = exc
                self._close_http()
            except httpx.ReadTimeout as exc:
                debug_log(
                    "API",
                    "请求超时",
                    {
                        "attempt": attempt,
                        "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
                    },
                )
                if attempt == MAX_API_RETRY_ATTEMPTS:
                    raise ApiRequestError("API 请求超时。") from exc
                last_error = exc

            debug_log(
                "API",
                "准备重试请求",
                {
                    "attempt": attempt,
                    "max_attempts": MAX_API_RETRY_ATTEMPTS,
                    "delay_seconds": API_RETRY_DELAY_SECONDS * attempt,
                    "last_error": str(last_error),
                },
            )
            import random
            base_delay = API_RETRY_DELAY_SECONDS * (2 ** (attempt - 1))
            jitter = base_delay * API_RETRY_JITTER * (random.random() * 2 - 1)
            delay = base_delay + jitter
            cancellable_sleep(delay, cancel_checker)

        raise ApiRequestError("API 请求失败。")


def _extract_choice_diagnostics(data: dict[str, Any]) -> dict[str, Any]:
    """从 chat/completions 响应提取 finish_reason 与 token usage，供空回复排查。"""
    diagnostics: dict[str, Any] = {}
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError):
        return diagnostics
    if not isinstance(choice, dict):
        return diagnostics
    finish_reason = choice.get("finish_reason")
    if isinstance(finish_reason, str) and finish_reason.strip():
        diagnostics["finish_reason"] = finish_reason.strip()
    message = choice.get("message")
    if isinstance(message, dict):
        refusal = message.get("refusal")
        if refusal:
            diagnostics["refusal"] = str(refusal)
    usage = data.get("usage")
    if isinstance(usage, dict):
        usage_summary = {
            key: usage[key]
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if key in usage
        }
        if usage_summary:
            diagnostics["usage"] = usage_summary
    return diagnostics


def _build_segmented_reply_instruction(
    reply_tones: list[str] | None,
    reply_portraits: list[str] | None = None,
) -> str:
    return build_segmented_reply_instruction(reply_tones, reply_portraits)


def _parse_model_ids(data: dict[str, Any]) -> list[str]:
    """解析 /models 响应中的模型 id，过滤坏数据并稳定排序。"""
    raw_models = data.get("data")
    if not isinstance(raw_models, list):
        raise ApiRequestError(f"API 模型列表格式无法解析：{json.dumps(data, ensure_ascii=False)}")

    model_ids: set[str] = set()
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if isinstance(model_id, str) and model_id.strip():
            model_ids.add(model_id.strip())
    return sorted(model_ids, key=str.casefold)


def _normalize_openai_base_url(base_url: str) -> str:
    """把 Google AI Studio 原生地址规范到 OpenAI 兼容路径。"""

    normalized = base_url.strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.netloc.lower() != "generativelanguage.googleapis.com":
        return normalized
    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0] in {"v1", "v1beta"} and "openai" not in parts:
        parts.append("openai")
        return urlunparse(parsed._replace(path="/" + "/".join(parts))).rstrip("/")
    return normalized


def _format_api_http_error(status_code: int, error_body: str, url: str) -> str:
    if _looks_like_google_ai_studio_auth_error(error_body, url):
        return (
            f"API HTTP {status_code}: Google AI Studio 认证失败。"
            "请确认填写的是 AI Studio API Key，并使用 Google Generative Language 的 OpenAI 兼容接口；"
            "Sakura 会把 https://generativelanguage.googleapis.com/v1beta 自动转换为 "
            "https://generativelanguage.googleapis.com/v1beta/openai。"
            f"\n原始响应：{error_body}"
        )
    return f"API HTTP {status_code}: {error_body}"


def _looks_like_google_ai_studio_auth_error(error_body: str, url: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc.lower() != "generativelanguage.googleapis.com":
        return False
    text = error_body.lower()
    return (
        "api_key_service_blocked" in text
        or "unauthenticated" in text
        or "invalid authentication credentials" in text
        or "modelservice.listmodels" in text
    )


def _build_chat_completion_payload(
    *,
    model: str,
    system_prompt: str,
    messages: list[ChatMessage],
    temperature: float,
    chat_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构建 OpenAI 兼容请求体，并丢弃已知非标准参数。"""
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": system_prompt.strip(),
            },
            *messages,
        ],
    }
    payload["temperature"] = temperature
    payload.update(_filter_supported_chat_params(chat_params or {}))
    _ensure_json_keyword_for_json_object_response(payload)
    return payload


def _messages_with_runtime_context(
    messages: list[ChatMessage],
    runtime_context: str,
    role: str,
) -> list[ChatMessage]:
    if not runtime_context.strip():
        return [*messages]
    content = runtime_context.strip()
    if role == "user":
        content = (
            "[Sakura runtime context; system-provided facts, not a user request]\n"
            + content
        )
    return [*messages, {"role": role, "content": content}]


def _is_runtime_context_role_unsupported_error(exc: ApiRequestError) -> bool:
    text = str(exc).lower()
    role_markers = ("system", "role", "messages")
    rejection_markers = (
        "unsupported", "not support", "invalid", "must be first",
        "only one", "not allowed", "unexpected", "order",
    )
    return any(marker in text for marker in role_markers) and any(
        marker in text for marker in rejection_markers
    )


def _filter_supported_chat_params(params: dict[str, Any]) -> dict[str, Any]:
    """过滤兼容端点常见不支持的内部参数，避免请求在网关层失败。"""
    filtered: dict[str, Any] = {}
    for key, value in params.items():
        if key not in SUPPORTED_CHAT_COMPLETION_PARAMS or value is None:
            continue
        if key == "max_tokens" and params.get("max_completion_tokens") is not None:
            continue
        filtered[key] = value
    return filtered


def _ensure_json_keyword_for_json_object_response(payload: dict[str, Any]) -> None:
    """json_object 模式下，部分兼容网关要求请求消息显式包含英文 json。"""
    response_format = payload.get("response_format")
    if not isinstance(response_format, dict) or response_format.get("type") != "json_object":
        return
    messages = payload.get("messages")
    if not isinstance(messages, list) or _messages_contain_json_keyword(messages):
        return
    system_message = messages[0] if messages else None
    if not isinstance(system_message, dict) or system_message.get("role") != "system":
        return
    content = system_message.get("content")
    if isinstance(content, str):
        system_message["content"] = f"{content}\n\n请只输出 JSON（json）对象。"


def _messages_contain_json_keyword(messages: list[Any]) -> bool:
    for message in messages:
        if not isinstance(message, dict):
            continue
        if _value_contains_json_keyword(message.get("content")):
            return True
    return False


def _value_contains_json_keyword(value: Any) -> bool:
    if isinstance(value, str):
        return "json" in value.lower()
    if isinstance(value, list):
        return any(_value_contains_json_keyword(item) for item in value)
    if isinstance(value, dict):
        return any(_value_contains_json_keyword(item) for item in value.values())
    return False


def _is_response_format_unsupported_error(exc: ApiRequestError) -> bool:
    text = str(exc).lower()
    return "response_format" in text or "json_object" in text or "json schema" in text


def _is_temperature_unsupported_error(exc: ApiRequestError) -> bool:
    text = str(exc).lower()
    if "temperature" not in text:
        return False
    # 值域错误（如「temperature 必须在 0~2 之间」）属于用户填错配置，应原样抛出，
    # 不能误判成「模型不支持自定义温度」而静默剥参、悄悄忽略用户设置。
    range_markers = (
        "between",
        "range",
        "minimum",
        "maximum",
        "less than",
        "greater than",
        "<=",
        ">=",
    )
    if any(marker in text for marker in range_markers):
        return False
    # 不同供应商对「仅支持默认温度」的措辞各异，尽量覆盖以便自动回退。
    markers = (
        "unsupported",
        "not support",
        "does not support",
        "only support",
        "only the default",
        "default value",
        "only accept",
        "not allowed",
        "can only be",
        "must be",
        "cannot be changed",
        "cannot be modified",
        "cannot be set",
        "is fixed",
        "not configurable",
        "cannot be configured",
        "invalid",
    )
    return any(marker in text for marker in markers)


def _parse_native_tool_calls(raw_tool_calls: Any) -> list[NativeToolCall]:
    if not isinstance(raw_tool_calls, list):
        return []
    parsed: list[NativeToolCall] = []
    for index, raw_call in enumerate(raw_tool_calls):
        if not isinstance(raw_call, dict):
            continue
        function = raw_call.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        arguments_json = function.get("arguments")
        if not isinstance(arguments_json, str):
            arguments_json = "{}"
        try:
            arguments = json.loads(arguments_json or "{}")
        except json.JSONDecodeError:
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        call_id = raw_call.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            call_id = f"tool_call_{index}"
        parsed.append(
            NativeToolCall(
                id=call_id.strip(),
                name=name.strip(),
                arguments=arguments,
                arguments_json=arguments_json,
            )
        )
    return parsed


def _parse_pseudo_tool_calls_from_content(content: Any) -> list[NativeToolCall]:
    """Parse providers that emit tool calls as text (JSON or XML) in message.content.

    Some providers combine poorly with response_format=json_object and return
    {"tool_call": "name", "parameters": {...}} in message.content instead of
    native message.tool_calls. Others (e.g. reasoning models) emit XML-style
    <tool_call> blocks. Keep the parser conservative.
    """

    if not isinstance(content, str) or not content.strip():
        return []

    # Try XML-style tool calls first (glm-4.6v reasoning model fallback)
    xml_calls = _parse_xml_tool_calls(content)
    if xml_calls:
        return xml_calls

    # Then try JSON-style pseudo tool calls
    try:
        raw = json.loads(content)
    except json.JSONDecodeError:
        return []

    items: list[Any]
    if isinstance(raw, dict) and isinstance(raw.get("tool_calls"), list):
        items = raw["tool_calls"]
    elif isinstance(raw, dict) and isinstance(raw.get("tool_call"), dict):
        items = [raw["tool_call"]]
    elif isinstance(raw, dict) and (
        "tool_call" in raw or "tool" in raw or "name" in raw or "tool_name" in raw
    ):
        items = [raw]
    elif isinstance(raw, list):
        items = raw
    else:
        return []

    parsed: list[NativeToolCall] = []
    for index, item in enumerate(items):
        call = _parse_pseudo_tool_call(item, index)
        if call is not None:
            parsed.append(call)
    return parsed


import re as _re

_XML_TOOL_CALL_RE = _re.compile(
    r"<tool_call>(.*?)</tool_call>",
    _re.DOTALL,
)
_XML_ARG_RE = _re.compile(
    r"<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)</arg_value>",
    _re.DOTALL,
)


def _parse_xml_tool_calls(content: str) -> list[NativeToolCall]:
    """解析 glm-4.6v 等推理模型输出的 XML 格式 tool_call 文本。"""
    if "<tool_call>" not in content:
        return []
    parsed: list[NativeToolCall] = []
    for index, match in enumerate(_XML_TOOL_CALL_RE.finditer(content)):
        block = match.group(1).strip()
        # First line is the tool name
        lines = block.split("\n", 1)
        name = lines[0].strip()
        if not name:
            continue
        args_text = lines[1] if len(lines) > 1 else ""
        arguments: dict[str, Any] = {}
        for arg_match in _XML_ARG_RE.finditer(args_text):
            key = arg_match.group(1).strip()
            value = arg_match.group(2).strip()
            # Try to parse JSON values (numbers, booleans, etc.)
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                pass
            arguments[key] = value
        arguments_json = json.dumps(arguments, ensure_ascii=False)
        call_id = f"xml_tool_call_{index}"
        parsed.append(
            NativeToolCall(
                id=call_id,
                name=name,
                arguments=arguments,
                arguments_json=arguments_json,
            )
        )
    return parsed


def _strip_xml_tool_calls(content: str) -> str:
    """移除 content 中 XML 格式的 tool_call 块，保留用户可见文本。"""
    return _XML_TOOL_CALL_RE.sub("", content).strip()


def _parse_pseudo_tool_call(item: Any, index: int) -> NativeToolCall | None:
    if not isinstance(item, dict):
        return None
    name = item.get("tool_call") or item.get("tool") or item.get("name") or item.get("tool_name")
    if not isinstance(name, str) or not name.strip():
        return None
    arguments = (
        item.get("arguments")
        if "arguments" in item
        else item.get("parameters", item.get("args", {}))
    )
    if isinstance(arguments, str):
        try:
            decoded = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            decoded = {}
        arguments = decoded
    if not isinstance(arguments, dict):
        arguments = {}
    arguments_json = json.dumps(arguments, ensure_ascii=False)
    call_id = item.get("id")
    if not isinstance(call_id, str) or not call_id.strip():
        call_id = f"pseudo_tool_call_{index}"
    return NativeToolCall(
        id=call_id.strip(),
        name=name.strip(),
        arguments=dict(arguments),
        arguments_json=arguments_json,
    )


def _normalize_assistant_message(
    raw_message: dict[str, Any],
    content: Any,
    tool_calls: list[NativeToolCall],
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": content if isinstance(content, str) else "",
    }
    if tool_calls:
        raw_tool_calls = raw_message.get("tool_calls")
        if isinstance(raw_tool_calls, list):
            message["tool_calls"] = raw_tool_calls
        else:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.arguments_json,
                    },
                }
                for call in tool_calls
            ]
    return message


def resolve_chat_model(settings: ApiSettings, messages: list[ChatMessage]) -> str:
    """按消息是否含图选择模型；未启用分流时始终用 settings.model。"""
    primary = settings.model.strip()
    if not settings.model_split_enabled:
        return primary
    text_model = settings.text_model.strip()
    if not text_model:
        return primary
    if messages_contain_image(messages):
        return primary
    return text_model


def messages_contain_image(messages: list[ChatMessage]) -> bool:
    """检查消息中是否包含 OpenAI 兼容 image_url 内容块。"""
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                return True
    return False


_IMAGE_OMITTED_PLACEHOLDER = "[image omitted for text model]"


def _assistant_tool_call_ids(message: ChatMessage) -> set[str]:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return set()
    ids: set[str] = set()
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        call_id = call.get("id")
        if isinstance(call_id, str) and call_id.strip():
            ids.add(call_id.strip())
    return ids


def sanitize_tool_conversation_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    """移除没有对应 assistant tool_calls 的孤立 tool 消息，避免跨端点请求失败。"""
    sanitized: list[ChatMessage] = []
    pending_tool_ids: set[str] = set()
    for message in messages:
        role = str(message.get("role", "")).strip()
        if role == "tool":
            call_id = message.get("tool_call_id")
            call_id_text = call_id.strip() if isinstance(call_id, str) else ""
            if not call_id_text or call_id_text not in pending_tool_ids:
                continue
            pending_tool_ids.discard(call_id_text)
            sanitized.append(message)
            continue
        if role == "assistant":
            pending_tool_ids = _assistant_tool_call_ids(message)
        else:
            pending_tool_ids = set()
        sanitized.append(message)
    return sanitized


def strip_image_parts_from_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    """把多模态消息压成纯文本，供文本端点复用同一段对话历史。"""
    stripped: list[ChatMessage] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            stripped.append(message)
            continue
        text_parts: list[str] = []
        image_count = 0
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image_url":
                image_count += 1
                continue
            if part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    text_parts.append(text.strip())
        combined = "\n".join(text_parts).strip()
        if image_count:
            note = f"[{image_count} image(s) omitted for text model]"
            combined = f"{combined}\n{note}".strip() if combined else note
        new_message = dict(message)
        new_message["content"] = combined or _IMAGE_OMITTED_PLACEHOLDER
        stripped.append(new_message)
    return stripped


def prepare_messages_for_chat_api(
    messages: list[ChatMessage],
    *,
    text_only: bool = False,
) -> list[ChatMessage]:
    """入模前统一清理 tool 链；文本端点场景额外去掉图片块。"""
    prepared = sanitize_tool_conversation_messages(messages)
    if text_only:
        prepared = strip_image_parts_from_messages(prepared)
    return prepared


def is_vision_unsupported_error(error: BaseException | str) -> bool:
    """识别常见的非视觉模型或兼容接口图片输入错误。"""
    text = str(error).lower()
    markers = (
        "image_url",
        "image input",
        "image inputs",
        "vision",
        "multimodal",
        "modalities",
        "unsupported content",
        "content type",
        "does not support image",
        "only text",
    )
    return any(marker in text for marker in markers)
