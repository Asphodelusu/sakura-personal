from __future__ import annotations

from types import SimpleNamespace

from app.agent.screen_observation import ScreenObservation, summarize_screen_observation


def _observation() -> ScreenObservation:
    return ScreenObservation(
        data_url="data:image/jpeg;base64,abc",
        width=320,
        height=180,
        captured_at="2026-08-24T00:59:44+08:00",
        screen_name="manual-selection",
    )


def test_summarize_screen_observation_uses_runtime_vision_client() -> None:
    called: list[str] = []

    class ChatCloud:
        def complete_raw(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            called.append("chat")
            raise AssertionError("截图摘要不得打到主 Chat")

    class VisionClient:
        def complete_raw(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            called.append("vision")
            return "聊天窗口里有一段代码。"

    runtime = SimpleNamespace(
        vision_api_client=VisionClient(),
        cloud_client=ChatCloud(),
    )

    summary = summarize_screen_observation(_observation(), runtime)

    assert summary == "聊天窗口里有一段代码。"
    assert called == ["vision"]


def test_summarize_screen_observation_does_not_use_chat_cloud_fallback() -> None:
    """主 Chat 是 RoutingLlmClient 时，不能把图发给 cloud_client（V4 Pro）。"""
    from app.llm.api_client import ApiSettings, OpenAICompatibleClient

    called: list[str] = []

    class ChatCloud(OpenAICompatibleClient):
        def __init__(self) -> None:
            super().__init__(
                ApiSettings(
                    base_url="https://api.deepseek.com",
                    api_key="key",
                    model="deepseek-v4-pro",
                )
            )

        def complete_raw(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            called.append("chat")
            return "不该走到这里"

    routing_chat = SimpleNamespace(cloud_client=ChatCloud())

    summary = summarize_screen_observation(_observation(), routing_chat)

    assert summary == ""
    assert called == []
