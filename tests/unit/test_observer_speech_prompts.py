"""Observer VLM/LLM prompts: 正向引导 + 来源标签。"""

from app.perception.observer import (
    _PROACTIVE_SYSTEM_PROMPT,
    _SPEECH_DECISION_INSTRUCTION,
)


def test_speech_decision_prioritizes_conversation_over_perception() -> None:
    text = _SPEECH_DECISION_INSTRUCTION
    assert "会話" in text
    assert "優先" in text or "最優先" in text
    assert "対話の既知" in text
    assert "デジタル生命" in text
    assert "[可见文字摘录]" in text
    assert "[画面摘要]" in text
    assert "[反应提示]" in text


def test_vlm_prompt_vision_only_compact_positive() -> None:
    text = _PROACTIVE_SYSTEM_PROMPT
    assert "观察者上下文" in text
    assert "デジタル生命" in text
    assert "彼" in text
    assert "visual_summary" in text
    assert "reaction_hint" in text
    assert "on_screen_text" in text
    assert "suggested_interval" in text
    assert "全文テキストは渡されない" in text
    assert "同一アプリ" in text
    assert "吹き出し" in text


def test_decision_uses_ta_not_customer_service() -> None:
    text = _SPEECH_DECISION_INSTRUCTION
    assert "彼" in text
    assert "他看着剧情" in text or "他" in text
    assert "我说的" in text
    assert "她自己的" in text
    assert "我看见的" in text
    assert "スクリーンで見た" in text


def test_speech_decision_teaches_source_labels() -> None:
    text = _SPEECH_DECISION_INSTRUCTION
    assert "即时通讯" in text or "别人" in text
    assert "游戏" in text


def test_vlm_user_content_omits_prior_observer_speech_history() -> None:
    import time

    from app.perception.observer import ObservationRecord, ProactiveConfig, ProactiveObserver

    observer = ProactiveObserver(
        api_base_url="https://example.com",
        api_key="x",
        api_model="m",
        config=ProactiveConfig(enabled=False),
    )
    observer._obs_history.append(
        ObservationRecord(
            timestamp=time.monotonic() - 30,
            window_title="Code",
            should_speak=True,
            reason="他完全没有回应午饭的问题",
            comment="昼ごはん、どうする？",
        )
    )
    text = observer._build_vlm_user_content(
        window_title="Code",
        idle_s=12,
        triggers=("timer",),
        now=time.monotonic(),
    )
    assert "活动窗口：Code" in text
    assert "[最近の観測履歴]" not in text
    assert "他完全没有回应" not in text
    assert "昼ごはん、どうする？" not in text
    assert "user did not answer" not in text.lower()
