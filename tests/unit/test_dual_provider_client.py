from __future__ import annotations

from app.llm.api_client import ApiSettings
from app.llm.dual_provider_client import DualProviderLlmClient, create_cloud_llm_client, is_dual_provider_client


def test_create_cloud_llm_client_returns_dual_provider_when_configured() -> None:
    settings = ApiSettings(
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key="zhipu-key",
        model="glm-5v-turbo",
        text_model="deepseek-v4-flash",
        model_split_enabled=True,
        dual_endpoint_enabled=True,
        text_base_url="https://api.deepseek.com",
        text_api_key="ds-key",
    )
    client = create_cloud_llm_client(settings)
    assert is_dual_provider_client(client)
    assert isinstance(client, DualProviderLlmClient)
    assert client.vision_client.settings.model == "glm-5v-turbo"
    assert client.text_client.settings.model == "deepseek-v4-flash"
    assert client.text_client.settings.base_url == "https://api.deepseek.com"


def test_dual_provider_routes_text_and_vision_messages() -> None:
    settings = ApiSettings(
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key="zhipu-key",
        model="glm-5v-turbo",
        text_model="deepseek-v4-flash",
        model_split_enabled=True,
        dual_endpoint_enabled=True,
        text_base_url="https://api.deepseek.com",
        text_api_key="ds-key",
    )
    client = DualProviderLlmClient(settings)
    text_messages = [{"role": "user", "content": "你好"}]
    image_messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
        }
    ]
    assert client._pick_client(text_messages) is client.text_client
    assert client._pick_client(image_messages) is client.vision_client


def test_dual_provider_sanitizes_orphan_tool_before_text_complete_with_tools(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = ApiSettings(
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key="zhipu-key",
        model="glm-5v-turbo",
        text_model="deepseek-v4-flash",
        model_split_enabled=True,
        dual_endpoint_enabled=True,
        text_base_url="https://api.deepseek.com",
        text_api_key="ds-key",
    )
    client = DualProviderLlmClient(settings)
    captured: dict[str, object] = {}

    def fake_complete_with_tools(system_prompt, messages, **kwargs):  # type: ignore[no-untyped-def]
        captured["messages"] = messages
        from app.llm.api_client import ChatCompletionTurn

        return ChatCompletionTurn(content='{"segments":[]}', tool_calls=[], message={"role": "assistant", "content": "{}"})

    monkeypatch.setattr(client.text_client, "complete_with_tools", fake_complete_with_tools)
    messages = [
        {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
        {"role": "user", "content": "继续"},
    ]
    client.complete_with_tools("system", messages, tools=[])
    sent_messages = captured["messages"]
    assert isinstance(sent_messages, list)
    assert sent_messages == [{"role": "user", "content": "继续"}]
