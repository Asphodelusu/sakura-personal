"""Observer VLM/LLM prompts: 分责 + 原文通道 + 对话事实优先。"""

from app.perception.observer import (
    _PROACTIVE_SYSTEM_PROMPT,
    _SPEECH_DECISION_INSTRUCTION,
)


def test_speech_decision_prioritizes_conversation_over_perception() -> None:
    text = _SPEECH_DECISION_INSTRUCTION
    assert "会話の事実を優先" in text or "会話事実" in text
    assert "もう一度聞かない" in text
    assert "対話の既知事実" in text
    assert "デジタル生命" in text
    assert "対等な他者" in text
    # 结构化观测包字段
    assert "[可见文字摘录]" in text
    assert "[画面摘要]" in text
    assert "[反应提示]" in text
    # 禁止脑补原文
    assert "捏造" in text or "補完して書いてはいけない" in text
    assert "可见文字摘录" in text
    # 短时印象：场面级、少抄私聊
    assert "場面レベル" in text


def test_vlm_prompt_vision_only_no_full_uia_pipeline() -> None:
    text = _PROACTIVE_SYSTEM_PROMPT
    assert "观察者上下文" in text
    assert "蒸し返さ" in text
    assert "デジタル生命" in text
    assert "対等な他者" in text
    assert "食事済み" in text
    # 新契约：结构化输出，不再要求独白抄原文
    assert "visual_summary" in text
    assert "reaction_hint" in text
    assert "on_screen_text" in text
    assert "inner_thought" not in text
    # 全文 UIA 不由 VLM 承担
    assert "全文テキストはあなたには渡されない" in text
    # 同应用内容切换 ≠ 慌乱切窗
    assert "同一アプリ" in text
    # 右下角桌宠 / 自己的台词不得当屏上内容
    assert "右下" in text
    assert "自分のセリフ" in text or "吹き出し" in text


def test_speech_decision_ignores_own_bubble_text() -> None:
    text = _SPEECH_DECISION_INSTRUCTION
    assert "自問自答" in text
    assert "吹き出し" in text
