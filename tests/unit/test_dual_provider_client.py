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
