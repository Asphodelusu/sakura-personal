"""Observer VLM/LLM prompts: conversation-fact priority + Sakura peer stance."""

from app.perception.observer import (
    _PROACTIVE_SYSTEM_PROMPT,
    _SPEECH_DECISION_INSTRUCTION,
)


def test_speech_decision_prioritizes_conversation_over_monologue() -> None:
    text = _SPEECH_DECISION_INSTRUCTION
    assert "会話の事実を優先" in text
    assert "もう一度聞かない" in text
    assert "対話の既知事実" in text
    assert "デジタル生命" in text
    assert "対等な他者" in text


def test_vlm_prompt_respects_observer_context_dialogue_facts() -> None:
    text = _PROACTIVE_SYSTEM_PROMPT
    assert "观察者上下文" in text
    assert "蒸し返さ" in text
    assert "デジタル生命" in text
    assert "対等な他者" in text
    assert "食事済み" in text
