"""Memory language policy + explicit source + curator emotion context."""

from __future__ import annotations

from app.agent.memory import DEFAULT_MEMORY_LANGUAGE_INSTRUCTIONS, MemoryStore
from app.agent.memory_curator import MemoryCurator, _SELF_CURATION_TASK_PROMPT
from app.agent.memory_reflector import _REFLECTION_SYSTEM_PROMPT


def test_memory_language_instructions_are_bilingual() -> None:
    text = DEFAULT_MEMORY_LANGUAGE_INSTRUCTIONS
    assert "简体中文" in text
    assert "日语" in text
    assert "他" in text
    assert "「我」" in text
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
    assert "「我」= 你自己" in text
    assert "「他」= 对方" in text
    assert "我自己的话归我" in text
    assert "心の記録" in text
    assert "绝不能" not in text  # 硬禁令不进任务正文；身份锚另有
    assert "不要用「主人」" not in text


def test_curator_prompt_treats_intimacy_as_memorable() -> None:
    """亲密关系按「人」记：里程碑高价值，过程流水账不堆。"""
    text = _SELF_CURATION_TASK_PROMPT
    assert "亲密关系" in text
    assert "第一次" in text
    assert "shared_moment" in text
    assert "0.85" in text
    assert "过程流水账" in text
    assert "今の関係" in text


def test_looks_like_third_person_self_detects_common_slips() -> None:
    from app.agent.memory_curator import looks_like_third_person_self

    assert looks_like_third_person_self("樱喜欢抹茶", "樱") is True
    assert looks_like_third_person_self("我对樱说今晚早点休息", "樱") is True
    assert looks_like_third_person_self("他叫我樱", "樱") is False
    assert looks_like_third_person_self("他喜欢抹茶", "樱") is False
    assert looks_like_third_person_self("我喜欢和他待着", "樱") is False


def test_curator_identity_anchor_maps_speakers(tmp_path) -> None:
    store = MemoryStore(base_dir=tmp_path, memory_client=object())
    curator = MemoryCurator(
        api_client=object(),
        memory_store=store,
        character_name="樱",
    )
    text = curator._build_self_curation_system_prompt()
    assert "你是「樱」" in text
    assert "「我」只能指你自己" in text
    assert "「他」指对方" in text
    assert "绝不能收成日记主语「我」" in text


def test_format_dialog_for_curation_uses_wo_ta_labels() -> None:
    from app.agent.memory_curator import _format_dialog_for_curation

    text = _format_dialog_for_curation(
        [
            {
                "created_at": "2026-07-21T12:00:00+08:00",
                "role": "user",
                "content": "我喜欢抹茶",
                "translation": "",
            },
            {
                "created_at": "2026-07-21T12:00:01+08:00",
                "role": "assistant",
                "content": "覚えたよ",
                "translation": "我记住了",
            },
        ]
    )
    assert "「我」=你自己" in text
    assert "他：我喜欢抹茶" in text
    assert "我：覚えたよ" in text
    assert "中文：我记住了" in text
    assert '"role"' not in text


def test_reflector_prompt_follows_bilingual_policy() -> None:
    text = _REFLECTION_SYSTEM_PROMPT
    assert "简体中文" in text
    assert "日语" in text
    assert "「我」=你自己" in text
    assert "「他」=对方" in text
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
    assert "他的情绪" in text
