# -*- coding: utf-8 -*-
"""短时屏幕印象：TTL、对话薄化、主对话 fragment。"""

from app.agent.sensory_context import build_sensory_impression_fragment
from app.perception.sensory_impression import (
    CHAT_MAX_CHARS,
    SensoryImpressionStore,
    sensory_impression_store,
    thin_impression_for_chat,
)


def setup_function() -> None:
    sensory_impression_store.clear()


def teardown_function() -> None:
    sensory_impression_store.clear()


def test_thin_strips_dialogue_facts_and_caps_length() -> None:
    text = (
        "相手が微信で友人とゲームの話をしている。"
        "対話の既知：さっき恋愛脳の話をしたばかり。"
    )
    out = thin_impression_for_chat(text, max_chars=160)
    assert "微信" in out
    assert "対話の既知" not in out
    assert "恋愛脳" not in out


def test_thin_hard_caps_long_scene() -> None:
    text = "場面：" + ("あ" * 300)
    out = thin_impression_for_chat(text, max_chars=CHAT_MAX_CHARS)
    assert len(out) <= CHAT_MAX_CHARS + 1  # allow ellipsis


def test_store_ttl_expires() -> None:
    store = SensoryImpressionStore(ttl_seconds=10.0)
    store.update("相手がQQで技術雑談をしている。", now=100.0)
    assert store.get_for_observer(now=105.0)
    assert store.get(now=111.0) is None
    assert store.get_for_chat(now=111.0) == ""


def test_store_clear() -> None:
    store = SensoryImpressionStore()
    store.update("相手がブラウザを見ている。")
    store.clear()
    assert store.get() is None


def test_chat_fragment_present_when_fresh() -> None:
    sensory_impression_store.update(
        "相手が微信で友人とチャット中。対話の既知：特になし。",
        spoken=False,
    )
    frag = build_sensory_impression_fragment()
    assert frag is not None
    assert frag.fragment_id == "runtime.sensory_impression"
    assert "短时屏幕印象" in frag.content
    assert "微信" in frag.content
    assert "対話の既知" not in frag.content
    assert frag.token_budget <= 128


def test_chat_fragment_absent_when_empty() -> None:
    assert build_sensory_impression_fragment() is None


def test_observer_gets_fuller_text_than_chat() -> None:
    full = (
        "相手がQQのグループでAI感情の実装について議論している。"
        "伝統的なデスクトップペットを参考にした発言があった。"
        "対話の既知：特になし。"
    )
    sensory_impression_store.update(full)
    obs = sensory_impression_store.get_for_observer()
    chat = sensory_impression_store.get_for_chat()
    assert "デスクトップペット" in obs or len(obs) >= len(chat)
    assert "対話の既知" not in chat
    assert len(chat) <= CHAT_MAX_CHARS + 1
