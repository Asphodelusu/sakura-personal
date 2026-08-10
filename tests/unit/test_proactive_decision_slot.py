"""Observer 决策 LLM 槽位：优先 chat_fast，回退 chat。"""

from __future__ import annotations

from types import SimpleNamespace

from app.ui.pet_window import PetWindow


def _settings(model: str) -> SimpleNamespace:
    return SimpleNamespace(
        base_url=f"https://example.test/{model}",
        api_key="sk-test",
        model=model,
    )


def test_resolve_proactive_decision_api_prefers_chat_fast() -> None:
    window = SimpleNamespace(
        agent_runtime=SimpleNamespace(
            chat_fast_api_client=SimpleNamespace(settings=_settings("deepseek-v4-flash")),
        ),
        api_client=SimpleNamespace(settings=_settings("deepseek-v4-pro")),
    )
    resolved = PetWindow._resolve_proactive_decision_api(window)
    assert resolved.model == "deepseek-v4-flash"


def test_resolve_proactive_decision_api_falls_back_to_chat() -> None:
    window = SimpleNamespace(
        agent_runtime=SimpleNamespace(chat_fast_api_client=None),
        api_client=SimpleNamespace(settings=_settings("deepseek-v4-pro")),
    )
    resolved = PetWindow._resolve_proactive_decision_api(window)
    assert resolved.model == "deepseek-v4-pro"
