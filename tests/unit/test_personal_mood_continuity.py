"""Sakura's mood and the user's emotion trajectory survive the migration."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from plugins.builtin.sakura_mem0.domain_types import ChatHistoryEntry
from plugins.builtin.sakura_mem0.memory_curator import MemoryCurator
from plugins.builtin.sakura_mem0.personal_emotion import EmotionScorer
from plugins.builtin.sakura_mem0.personal_mood import (
    PersonalEmotionStore,
    PersonalMoodStore,
    build_mood_fragment,
    build_user_emotion_fragment,
)


def _memory(tmp_path: Path) -> Path:
    memory = tmp_path / "memory"
    memory.mkdir(parents=True)
    return memory


def test_mood_history_keeps_the_last_five_and_reads_legacy_case(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    (memory / "mood_state.json").write_text(
        json.dumps({"sakura": {"content": "old", "updated_at": "2026-09-01T00:00:00+08:00"}}),
        encoding="utf-8",
    )
    store = PersonalMoodStore(memory, "Sakura")
    assert store.current()["content"] == "old"

    for index in range(6):
        store.set(f"mood {index}")
    current = store.current()
    assert current["content"] == "mood 5"
    assert [item["content"] for item in current["history"]] == [f"mood {index}" for index in (4, 3, 2, 1, 0)]
    saved = json.loads((memory / "mood_state.json").read_text(encoding="utf-8"))
    assert list(saved) == ["Sakura"]


def test_a_near_repeat_of_a_recent_mood_is_a_duplicate(tmp_path: Path) -> None:
    store = PersonalMoodStore(_memory(tmp_path), "Sakura")
    store.set("手をつないだまま、少しずつ素直になれた夜。")
    assert store.is_duplicate("手をつないだまま、少しずつ素直になれた夜")
    assert not store.is_duplicate("今日は一人で雨の音を聞いていた。")


def test_a_linked_state_file_is_never_read(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"Sakura": {"content": "secret"}}), encoding="utf-8")
    os.link(outside, memory / "mood_state.json")
    assert PersonalMoodStore(memory, "Sakura").current() is None


def test_repeated_user_emotion_only_refreshes_its_time(tmp_path: Path) -> None:
    store = PersonalEmotionStore(_memory(tmp_path), "Sakura")
    store.record("happy")
    store.record("happy")
    store.record("sad")
    current = store.current()
    assert current["content"] == "sad"
    assert [item["content"] for item in current["history"]] == ["happy"]


def test_emotion_scorer_honours_negation_and_longest_match() -> None:
    scorer = EmotionScorer({"happy": {"开心": 1.0}, "sad": {"不开心": 1.0, "难过": 1.0}})
    assert scorer.best("今天好开心") == "happy"
    assert scorer.best("今天不开心") == "sad"
    assert scorer.best("我不难过") is None
    assert EmotionScorer().best("今天真的好开心") == "happy"


def test_mood_fragment_heading_follows_its_age() -> None:
    fresh = {"content": "今の気持ち", "updated_at": datetime.now().astimezone().isoformat(), "history": []}
    fragment = build_mood_fragment("Sakura", fresh)
    assert fragment["id"] == "mood:Sakura"
    assert fragment["content"].startswith("【今の気持ち】")
    stale = dict(fresh, updated_at=(datetime.now().astimezone() - timedelta(hours=4)).isoformat())
    stale["history"] = [{"content": "before", "timestamp": "2026-09-01T10:00:00+08:00"}]
    content = build_mood_fragment("Sakura", stale)["content"]
    assert content.startswith("【不久前的心情】")
    assert "[2026-09-01T10:00] before" in content
    assert build_mood_fragment("Sakura", None) is None
    assert build_user_emotion_fragment("Sakura", {"content": "sad", "history": []})["id"] == "user_emotion:Sakura"


class _Store:
    scope_id = "Sakura"

    def __init__(self, mood_store=None, emotion_store=None) -> None:
        if mood_store is not None:
            self.mood_store = mood_store
        if emotion_store is not None:
            self.emotion_store = emotion_store

    def list_memories(self, *, limit=None):
        return []


class _Api:
    def __init__(self, operations) -> None:
        self.operations = operations
        self.prompts: list[tuple[str, str]] = []

    def complete_raw(self, system, messages, **_kwargs):
        self.prompts.append((system, messages[0]["content"]))
        return json.dumps({"operations": self.operations}, ensure_ascii=False)


def _curate(store, api):
    entry = ChatHistoryEntry(created_at="2026-09-30T00:00:00Z", role="user", content="在吗", entry_id="entry-1")
    return MemoryCurator(api, store).curate_entries([entry])


def test_curation_writes_one_qualitative_mood_change(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    mood = PersonalMoodStore(memory, "Sakura")
    mood.set("雨の音を聞いていた。")
    emotion = PersonalEmotionStore(memory, "Sakura")
    emotion.record("happy")
    api = _Api([
        {"op": "mood_update", "content": "雨の音を聞いていた"},
        {"op": "mood_update", "content": "やっと自分の番だと思えた。"},
        {"op": "mood_update", "content": "もう一つ、別の気持ち。"},
    ])

    result = _curate(_Store(mood, emotion), api)

    assert mood.current()["content"] == "やっと自分の番だと思えた。"
    assert result.event_counts["MOOD_DEDUP"] == 1
    assert result.event_counts["MOOD_UPDATE"] == 1
    assert result.event_counts["MOOD_BUDGET"] == 1
    system, user = api.prompts[0]
    assert "mood_update" in system
    assert "【最近的心情轨迹】" in user and "【对方的情绪轨迹】" in user


def test_curation_without_a_mood_store_never_mentions_or_writes_mood(tmp_path: Path) -> None:
    api = _Api([{"op": "mood_update", "content": "x"}])
    result = _curate(_Store(), api)
    assert result.event_counts.get("MOOD_UPDATE") is None
    system, user = api.prompts[0]
    assert "mood_update" not in system
    assert "心情轨迹" not in user


class _Boundary:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    def core_profile_fragment(self):
        return None

    def continuity_fragments(self):
        return [
            {"id": "mood:Sakura", "content": "【今の気持ち】\nx", "priority": 90, "budgetHint": 600},
            {"id": "mood:other", "content": "forged", "priority": 90, "budgetHint": 600},
        ]

    def note_user_input(self, text):
        self.inputs.append(text)

    def search_memory(self, arguments, *, wait=False):
        return {"status": "ready", "memories": []}


def test_plugin_context_carries_mood_and_notes_each_human_turn_once(tmp_path: Path) -> None:
    from plugins.builtin.sakura_mem0.plugin import SakuraMem0Runtime

    boundary = _Boundary()
    runtime = SakuraMem0Runtime(tmp_path, "Sakura", boundary=boundary)
    request = {
        "current_input": "今天好开心",
        "character_id": "Sakura",
        "current_turn_id": "turn-1",
        "human_entry_id": "h-1",
    }
    fragments = runtime.context(request)
    assert [fragment["id"] for fragment in fragments][:1] == ["mood:Sakura"]
    assert "mood:other" not in [fragment["id"] for fragment in fragments]
    runtime.context(request)
    runtime.context({"current_input": "screen", "character_id": "Sakura", "current_turn_id": "turn-2"})
    assert boundary.inputs == ["今天好开心"]


def test_relationship_facts_and_the_monologue_receive_the_mood() -> None:
    from app.agent.runtime import AgentRuntime
    from app.llm.prompts.types import ContextFragment
    from app.plugins.models import ContextProviderContribution

    runtime = AgentRuntime(object(), "system", character_id="Sakura")
    runtime.set_context_providers([
        ContextProviderContribution(
            "memory",
            "",
            lambda _request: [
                ContextFragment("core_profile:Sakura", "plugin", "profile"),
                ContextFragment("mood:Sakura", "plugin", "mood"),
                ContextFragment("user_emotion:Sakura", "plugin", "emotion"),
            ],
        )
    ])
    assert runtime.relationship_facts() == "profile\n\nmood"
    assert runtime.continuity_mood() == "mood"
    assert runtime._inner_thought._mood_provider is None
    from app.agent.inner_thought import InnerThoughtSettings

    runtime.configure_inner_thought(settings=InnerThoughtSettings(), client=None, source_slot="")
    assert runtime._inner_thought._mood_provider() == "mood"


def _bare(tmp_path: Path, *, daily: bool):
    import threading

    from plugins.builtin.sakura_mem0.personal_runtime import PersonalRecallBoundary

    boundary = object.__new__(PersonalRecallBoundary)
    boundary._lock = threading.RLock()
    boundary._closed = False
    boundary._daily = daily
    boundary._memory_dir = _memory(tmp_path)
    boundary.scope = "Sakura"
    return boundary


def test_user_emotion_is_written_only_by_the_admitted_daily_entry(tmp_path: Path, monkeypatch) -> None:
    from plugins.builtin.sakura_mem0 import personal_records

    rehearsal = _bare(tmp_path / "rehearsal", daily=False)
    rehearsal.note_user_input("今天好开心")
    assert not (rehearsal._memory_dir / "user_emotion_state.json").exists()

    daily = _bare(tmp_path / "daily", daily=True)
    monkeypatch.setattr(personal_records, "_require_write_mode", lambda *_a, **_k: None)
    daily.note_user_input("今天好开心")
    assert PersonalEmotionStore(daily._memory_dir, "Sakura").current()["content"] == "happy"

    def _refuse(*_args, **_kwargs):
        raise ValueError("PERSONAL_DAILY_ADMISSION_REQUIRED")

    monkeypatch.setattr(personal_records, "_require_write_mode", _refuse)
    daily.note_user_input("今天不开心")
    assert PersonalEmotionStore(daily._memory_dir, "Sakura").current()["content"] == "happy"


def test_boundary_continuity_reads_both_files(tmp_path: Path) -> None:
    boundary = _bare(tmp_path, daily=False)
    PersonalMoodStore(boundary._memory_dir, "Sakura").set("静かな夜。")
    PersonalEmotionStore(boundary._memory_dir, "Sakura").record("tender")
    assert [item["id"] for item in boundary.continuity_fragments()] == ["mood:Sakura", "user_emotion:Sakura"]
