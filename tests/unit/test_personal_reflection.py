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


def test_closed_scheduler_does_not_start_or_consume_interval(tmp_path):
    calls = []
    scheduler = ReflectionScheduler(tmp_path, store_factory=lambda: calls.append('store'),
                                    client_factory=lambda: calls.append('client'))
    scheduler.close()
    assert scheduler.maybe_start() is False
    scheduler.run_once()
    assert calls == []
    assert not ReflectionStateStore(tmp_path).snapshot().last_reflection_at


def test_shutdown_cancels_inflight_reply_before_writing(tmp_path):
    import threading

    entered, cancelled = threading.Event(), threading.Event()
    store = _Store(_facts())

    class BlockingClient:
        closed = False

        def complete_raw(self, *_args, cancel_checker=None, **_kwargs):
            entered.set()
            assert cancelled.wait(5)
            # Even a transport that returns a late reply must not write it.
            return _reply('late reflection')

        def close(self):
            self.closed = True

    client = BlockingClient()
    scheduler = ReflectionScheduler(tmp_path, store_factory=lambda: store,
                                    client_factory=lambda: client)
    assert scheduler.maybe_start()
    assert entered.wait(5)
    original_join = scheduler.join

    def release_then_join(timeout=None):
        cancelled.set()
        original_join(timeout)

    scheduler.join = release_then_join
    try:
        scheduler.close()
        assert store.created == []
        assert client.closed
        assert not ReflectionStateStore(tmp_path).snapshot().last_reflection_at
        assert scheduler.maybe_start() is False
    finally:
        cancelled.set()
        original_join(5)


def test_personal_boundary_stops_reflection_before_closing_shared_store():
    import threading
    from types import SimpleNamespace
    from plugins.builtin.sakura_mem0.personal_runtime import PersonalRecallBoundary

    events = []
    boundary = object.__new__(PersonalRecallBoundary)
    boundary._lock = threading.Lock()
    boundary._closed = False
    boundary._status = 'ready'
    boundary._pending_timeline = object()
    boundary._thread = SimpleNamespace(join=lambda: events.append('loader'))
    boundary._reflection = SimpleNamespace(close=lambda: events.append('reflection'))
    boundary.recall_policy = SimpleNamespace(close=lambda: events.append('recall'))
    boundary._curation = SimpleNamespace(close=lambda: events.append('store'))
    boundary._records = object()

    boundary.close()

    assert events == ['loader', 'reflection', 'recall', 'store']
    assert boundary._closed and boundary._records is None


def test_reflection_admission_excludes_curation_and_model_tasks(tmp_path, monkeypatch):
    import pytest
    from test_core_host_memory import FakeMemoryStore, _boundary, _timeline
    from plugins.builtin.sakura_mem0.boundary import MemoryBoundaryError

    owner = _boundary(tmp_path, FakeMemoryStore(), config={'triggerTurns': 1})
    timeline = _timeline(tmp_path)
    started = []
    monkeypatch.setattr(owner, '_start_curation', lambda *args: started.append(args))
    try:
        assert owner.try_begin_reflection()
        assert not owner.try_begin_reflection()
        owner.note_timeline_changed(timeline)
        assert started == []
        with pytest.raises(MemoryBoundaryError) as error:
            owner.begin_model_download('model-task')
        assert error.value.code == 'MEMORY_TASK_BUSY'
        owner.finish_reflection()
        assert len(started) == 1
        assert not owner.try_begin_reflection()
    finally:
        owner.close()
    assert not owner.try_begin_reflection()


def test_reflection_releases_admission_after_failure(tmp_path):
    from test_core_host_memory import FakeMemoryStore, _boundary

    owner = _boundary(tmp_path, FakeMemoryStore())
    class BrokenClient:
        def complete_raw(self, *_args, **_kwargs):
            raise TimeoutError()

    scheduler = ReflectionScheduler(tmp_path, store_factory=lambda: _Store(_facts()),
                                    client_factory=BrokenClient,
                                    try_begin=owner.try_begin_reflection,
                                    finish=owner.finish_reflection)
    try:
        assert scheduler.maybe_start()
        scheduler.join(5)
        assert owner.try_begin_reflection()
        owner.finish_reflection()
        scheduler.close()
        assert not scheduler.maybe_start()
        assert owner.try_begin_reflection()
        owner.finish_reflection()
    finally:
        owner.close()


def test_thread_start_failure_releases_reflection_admission(tmp_path, monkeypatch):
    import pytest
    from test_core_host_memory import FakeMemoryStore, _boundary
    from plugins.builtin.sakura_mem0 import personal_reflection

    owner = _boundary(tmp_path, FakeMemoryStore())
    scheduler = ReflectionScheduler(tmp_path, store_factory=lambda: _Store(_facts()),
                                    client_factory=lambda: _Client(_reply()),
                                    try_begin=owner.try_begin_reflection,
                                    finish=owner.finish_reflection)
    def fail_start(_thread):
        raise RuntimeError('synthetic thread start failure')
    monkeypatch.setattr(personal_reflection.threading.Thread, 'start', fail_start)
    try:
        with pytest.raises(RuntimeError, match='synthetic thread start failure'):
            scheduler.maybe_start()
        scheduler.close()
        assert owner.try_begin_reflection()
        owner.finish_reflection()
    finally:
        owner.close()


def test_personal_close_does_not_restart_deferred_curation(tmp_path, monkeypatch):
    import threading
    from test_core_host_memory import FakeMemoryStore, _boundary, _timeline
    from plugins.builtin.sakura_mem0.personal_runtime import PersonalRecallBoundary

    owner = _boundary(tmp_path, FakeMemoryStore(), config={'triggerTurns': 1})
    personal = object.__new__(PersonalRecallBoundary)
    personal._lock = threading.RLock()
    personal._closed = True
    personal._curation = owner
    started = []
    monkeypatch.setattr(owner, '_start_curation', lambda *args: started.append(args))
    try:
        assert owner.try_begin_reflection()
        owner.note_timeline_changed(_timeline(tmp_path))
        personal._finish_reflection()
        assert started == []
    finally:
        owner.close()


def test_cancelled_meta_reflection_preserves_completed_first_order(tmp_path):
    import threading

    entered, release = threading.Event(), threading.Event()
    reflections = [{'content': f'感想{index}', 'category': 'reflection'} for index in range(6)]
    store = _Store([*_facts(), *reflections])
    class Client:
        calls = 0
        def complete_raw(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return _reply()
            entered.set()
            assert release.wait(5)
            return _reply('late meta reflection')

    scheduler = ReflectionScheduler(tmp_path, store_factory=lambda: store,
                                    client_factory=Client, clock=lambda: NOW)
    assert scheduler.maybe_start()
    assert entered.wait(5)
    original_join = scheduler.join
    def release_then_join(timeout=None):
        release.set()
        original_join(timeout)
    scheduler.join = release_then_join
    try:
        scheduler.close()
        saved = ReflectionStateStore(tmp_path).snapshot()
        assert saved.total_reflections == 1
        assert saved.last_reflection_at == NOW.isoformat()
        assert saved.meta_reflection_source_count == 0
        assert saved.total_meta_created == 0 and saved.last_meta_reflection_at == ''
        assert store.created == []
    finally:
        release.set()
        original_join(5)
