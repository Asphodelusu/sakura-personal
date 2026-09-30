"""Solitary reflection returns: every 8 hours, few, deduplicated, never fed back."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from plugins.builtin.sakura_mem0.memory import _memory_metadata
from plugins.builtin.sakura_mem0.personal_reflection import (
    PersonalReflector,
    ReflectionScheduler,
    ReflectionStateStore,
    reflection_due,
)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


class _Store:
    def __init__(self, memories):
        self.memories = list(memories)
        self.created = []

    def list_memories(self, *, limit=None):
        return list(self.memories)

    def create_memory(self, arguments, *, allow_sensitive=False):
        assert allow_sensitive is True
        self.created.append(dict(arguments))
        self.memories.append({"content": arguments["content"], "category": arguments["category"],
                              "metadata": {"memory_kind": arguments["memory_kind"]}})
        return {"ok": True}


class _Client:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []

    def complete_raw(self, system_prompt, messages, **_kwargs):
        self.prompts.append((system_prompt, messages))
        return self.responses.pop(0)


def _facts(count=5):
    return [{"content": f"fact {index}", "layer": "semantic", "importance": 0.5} for index in range(count)]


def _reply(*contents):
    return json.dumps({"reflections": [{"content": text, "importance": 0.7, "confidence": 0.6} for text in contents]},
                      ensure_ascii=False)


def test_reflection_writes_at_most_two_new_impressions(tmp_path: Path) -> None:
    earlier = {"content": "我注意到他最近总是熬夜改东西。", "category": "reflection"}
    store = _Store([*_facts(), earlier])
    client = _Client(_reply("我注意到他最近总是熬夜改东西", "他最近很在意进度。", "第三条"))

    created, skipped, empty = PersonalReflector(client, store).reflect()

    assert (created, skipped, empty) == (1, 1, False)
    assert store.created[0]["memory_kind"] == "reflection"
    assert store.created[0]["source"] == "reflection"
    assert earlier["content"] not in client.prompts[0][1][0]["content"]


def test_too_few_memories_skip_the_model(tmp_path: Path) -> None:
    client = _Client()
    assert PersonalReflector(client, _Store(_facts(4))).reflect() == (0, 0, True)
    assert client.prompts == []


def test_invalid_json_gets_one_repair_request() -> None:
    client = _Client("not json", _reply("他最近很在意进度。"))
    created, _skipped, _empty = PersonalReflector(client, _Store(_facts())).reflect()
    assert created == 1
    assert len(client.prompts) == 2


def test_scheduler_respects_the_interval_and_distills_meta_reflections(tmp_path: Path) -> None:
    reflections = [{"content": f"感想{index}", "category": "reflection"} for index in range(6)]
    store = _Store([*_facts(), *reflections])
    client = _Client(_reply(), _reply("我现在是会主动表达在乎的人。"))
    state = ReflectionStateStore(tmp_path)
    scheduler = ReflectionScheduler(tmp_path, store_factory=lambda: store, client_factory=lambda: client,
                                    clock=lambda: NOW)

    scheduler.run_once()

    saved = state.snapshot()
    assert saved.total_reflections == 1 and saved.last_empty is True
    assert saved.meta_reflection_source_count == 6 and saved.total_meta_created == 1
    assert store.created[-1]["memory_kind"] == "meta_reflection"
    assert not reflection_due(saved, now=NOW + timedelta(hours=7))
    assert reflection_due(saved, now=NOW + timedelta(hours=8))
    assert scheduler.maybe_start() is False


def test_a_failed_pass_still_waits_for_the_next_interval(tmp_path: Path) -> None:
    class Broken:
        def complete_raw(self, *_args, **_kwargs):
            raise TimeoutError()

    scheduler = ReflectionScheduler(tmp_path, store_factory=lambda: _Store(_facts()),
                                    client_factory=lambda: Broken(), clock=lambda: NOW)
    scheduler.run_once()
    assert ReflectionStateStore(tmp_path).snapshot().last_reflection_at == NOW.isoformat()


def test_memory_metadata_keeps_kind_time_and_emotion() -> None:
    metadata = _memory_metadata({
        "content": "x",
        "memory_kind": "commitment",
        "event_time": "2026-10-01",
        "valid_until": "2026-10-02",
        "emotion": "HAPPY",
        "volatile": True,
        "status": "Released",
    }, scope_id="alice")
    assert metadata["memory_kind"] == "commitment"
    assert metadata["event_time"] == "2026-10-01"
    assert metadata["valid_until"] == "2026-10-02"
    assert metadata["emotion"] == "happy"
    assert metadata["volatile"] is True
    assert metadata["status"] == "released"
