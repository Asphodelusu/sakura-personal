"""LLM 连接预热：去重、静默失败、可与对话并发（无全局锁）。"""

from __future__ import annotations

from types import SimpleNamespace

from app.core.llm_warmup import warm_agent_runtime_llm_connections, warm_llm_clients
from app.llm.api_client import ApiSettings, OpenAICompatibleClient


def test_warm_connection_skips_without_credentials() -> None:
    client = OpenAICompatibleClient(
        ApiSettings(base_url="", api_key="", model="m")
    )
    assert client.warm_connection() is False


def test_warm_connection_treats_http_error_status_as_success(monkeypatch) -> None:
    client = OpenAICompatibleClient(
        ApiSettings(base_url="https://example.com/v1", api_key="sk-test", model="m")
    )

    class FakeResponse:
        status_code = 401

    class FakeHttp:
        def request(self, *_a, **_k):
            return FakeResponse()

    monkeypatch.setattr(client, "_http_client", lambda: FakeHttp())
    assert client.warm_connection(timeout_seconds=1.0) is True


def test_warm_llm_clients_dedupes_same_endpoint() -> None:
    calls: list[str] = []

    class FakeClient:
        def __init__(self, name: str) -> None:
            self.settings = SimpleNamespace(
                base_url="https://api.example/v1",
                api_key="sk-shared-key",
            )
            self.name = name

        def warm_connection(self, *, timeout_seconds: float = 8.0) -> bool:
            calls.append(self.name)
            return True

    a = FakeClient("a")
    b = FakeClient("b")
    assert warm_llm_clients(a, b) == 1
    assert calls == ["a"]


def test_warm_agent_runtime_covers_chat_and_fast() -> None:
    calls: list[str] = []

    class FakeClient:
        def __init__(self, name: str, host: str) -> None:
            self.settings = SimpleNamespace(base_url=f"https://{host}/v1", api_key="k")
            self.name = name

        def warm_connection(self, *, timeout_seconds: float = 8.0) -> bool:
            calls.append(self.name)
            return True

    runtime = SimpleNamespace(
        api_client=FakeClient("chat", "chat.example"),
        chat_fast_api_client=FakeClient("fast", "fast.example"),
    )
    assert warm_agent_runtime_llm_connections(runtime) == 2
    assert calls == ["chat", "fast"]
