"""Memory language policy + explicit source + curator emotion context."""

from __future__ import annotations

from app.agent.memory import DEFAULT_MEMORY_LANGUAGE_INSTRUCTIONS, MemoryStore
from app.agent.memory_curator import MemoryCurator, _SELF_CURATION_TASK_PROMPT
from app.agent.memory_reflector import _REFLECTION_SYSTEM_PROMPT


def test_memory_language_instructions_are_bilingual() -> None:
    text = DEFAULT_MEMORY_LANGUAGE_INSTRUCTIONS
    assert "简体中文" in text
    assert "日语" in text
    assert "对方" in text
    assert "自己的内心" in text or "心の記録" in text
    assert "日记" in text


def test_curator_prompt_uses_unified_core_profile_sections() -> None:
    text = _SELF_CURATION_TASK_PROMPT
    assert "今の関係" in text
    assert "今の私" in text
    assert "两侧记忆" in text or "语言约定" in text
    assert "関係の記録" not in text
    assert "あなたについて知っていること" not in text


def test_curator_prompt_requires_fact_discipline() -> None:
    text = _SELF_CURATION_TASK_PROMPT
    assert "谁对谁说了什么" in text
    assert "你自己的话归你" in text
    assert "心の記録" in text
    assert "绝不能" not in text
    assert "不要用「主人」" not in text


def test_reflector_prompt_follows_bilingual_policy() -> None:
    text = _REFLECTION_SYSTEM_PROMPT
    assert "简体中文" in text
    assert "日语" in text
    assert "不要用「主人」" not in text


def test_remember_memory_defaults_source_to_explicit(tmp_path) -> None:
    class FakeMem0:
        def __init__(self) -> None:
            self.meta = None

        def add(self, content, *, user_id, metadata, infer=False):
            self.meta = metadata
            return {
                "results": [
                    {
                        "id": "m1",
                        "content": content,
                        "memory": content,
                        "metadata": metadata,
                    }
                ]
            }

    fake = FakeMem0()
    store = MemoryStore(base_dir=tmp_path, memory_client=fake)
    result = store.remember_memory({"content": "对方喜欢抹茶"})
    assert result["ok"] is True
    assert fake.meta is not None
    assert fake.meta["source"] == "explicit"


def test_curator_emotion_block_includes_current(tmp_path) -> None:
    store = MemoryStore(base_dir=tmp_path, memory_client=object())
    store._save_user_emotion_state(
        {
            store.scope_id: {
                "content": "anxious",
                "updated_at": "2026-07-20T12:00:00",
                "history": [
                    {"content": "happy", "timestamp": "2026-07-20T11:00:00"},
                ],
            }
        }
    )
    curator = MemoryCurator(api_client=object(), memory_store=store)
    text = curator._load_user_emotion_history_text()
    assert "当前：anxious" in text
    assert "happy" in text
