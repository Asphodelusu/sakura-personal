"""Completed-chat events through the actual personal store and Timeline cursor."""
import json
import threading
from types import SimpleNamespace

import pytest

from test_personal_memory_backend import dependencies
from test_personal_model_loading import model_fixture
from test_personal_plugin_runtime import Context
from test_core_host_memory import _timeline
from app.legacy_import.personal_copy import prepare_personal_copy, STATE_FILE
from plugins.builtin.sakura_mem0 import plugin


@pytest.fixture
def rehearsal(tmp_path, dependencies, monkeypatch):
    source, work = tmp_path / 'source', tmp_path / 'work'
    _, snapshot, _, _ = model_fixture(source / 'data', dependencies, monkeypatch, 384)
    prepare_personal_copy(source, work, source_is_quiescent=lambda: True)
    memory = work / 'data/memory'
    (memory / STATE_FILE).write_text(json.dumps({'state': 'complete'}))
    (memory / '.personal-write-rehearsal.json').write_text(json.dumps({
        'purpose': 'personal-memory-write-rehearsal', 'root': str(memory.resolve())}))
    timeline = _timeline(work)
    class Services(Context):
        def __init__(self):
            super().__init__(work)
            self.events = {}
            self.config = SimpleNamespace(get=lambda: {'personalSnapshot': str(snapshot),
                'triggerTurns': 1, 'curationProfileId': 'fixture', 'curationModel': 'curator'})
        def on(self, name, callback):
            self.events[name] = callback
        def get(self, name):
            if name == 'sakura.host.character':
                return SimpleNamespace(current=lambda: {'id': 'sakura', 'systemPrompt': 'Synthetic persona'})
            if name == 'sakura.host.timeline':
                return timeline
            if name == 'sakura.host.model_slots':
                return SimpleNamespace(catalog=lambda: [{'id': 'fixture', 'alias': 'Fixture', 'models': ['curator']}],
                    resolve=lambda _: {'profileId': 'fixture', 'model': 'curator',
                                       'baseUrl': 'https://example.invalid/v1', 'apiKey': 'synthetic', 'timeoutSeconds': 60})
            return super().get(name)
    context = Services()
    calls = []
    class Client:
        def __init__(self, *_args, **_kwargs):
            pass
        def complete_raw(self, *args, **kwargs):
            calls.append(1)
            return json.dumps({'operations': [{'op': 'add', 'content': '用户喜欢樱花', 'layer': 'semantic'}]})
        def close(self):
            pass
    monkeypatch.setattr('plugins.builtin.sakura_mem0.boundary.OpenAICompatibleClient', Client)
    yield context, timeline, calls, Client
    for effect in reversed(context.effects):
        effect()


def start(context):
    plugin.PersonalWriteRehearsalPlugin().setup(context)
    runtime = context.tools[-1][1].__self__
    runtime._boundary._thread.join(5)
    assert runtime._boundary._status == 'ready'
    return runtime._boundary


def event(context, timeline, character='sakura'):
    context.events[plugin.HOST_CHAT_COMPLETED_EVENT]({'characterId': character,
        'turnId': 'completed-turn', 'cursor': timeline.store.latest_cursor('sakura')})


def joined(boundary):
    for thread in list(boundary._curation._curation_threads._threads):
        thread.join(5)
        assert not thread.is_alive()


def test_completed_event_persists_memory_and_cursor_without_duplicate(rehearsal):
    context, timeline, calls, _ = rehearsal
    boundary = start(context)
    event(context, timeline, 'bob')
    assert calls == []
    event(context, timeline)
    joined(boundary)
    assert calls == [1]
    assert len(boundary._records.list('sakura')) == 1
    assert boundary._curation._curation_state.curation_cursor() == timeline.store.latest_cursor('sakura')
    event(context, timeline)
    joined(boundary)
    assert calls == [1]
    boundary.close()
    restarted = start(context)
    event(context, timeline)
    joined(restarted)
    assert calls == [1]


def test_write_failure_does_not_advance_cursor(rehearsal, monkeypatch):
    context, timeline, calls, _ = rehearsal
    boundary = start(context)
    original = boundary._records.create
    def fail(*args, **kwargs):
        raise OSError('synthetic failure before mutation')
    monkeypatch.setattr(boundary._records, 'create', fail)
    event(context, timeline)
    joined(boundary)
    assert calls == [1]
    assert boundary._curation._curation_state.curation_cursor() == ''
    monkeypatch.setattr(boundary._records, 'create', original)
    event(context, timeline)
    joined(boundary)
    assert calls == [1, 1]
    assert boundary._curation._curation_state.curation_cursor() == timeline.store.latest_cursor('sakura')


def test_close_cancels_active_curation_before_cursor_commit(rehearsal, monkeypatch):
    context, timeline, _, client = rehearsal
    entered, release = threading.Event(), threading.Event()
    original = client.complete_raw
    def blocked(self, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(client, 'complete_raw', blocked)
    boundary = start(context)
    event(context, timeline)
    assert entered.wait(5)
    closing = threading.Thread(target=boundary.close)
    closing.start()
    assert boundary._curation._curation_cancel.wait(5)
    release.set()
    closing.join(5)
    assert not closing.is_alive()
    assert boundary._curation._curation_state.curation_cursor() == ''


def test_completed_event_during_loading_is_processed_after_ready(rehearsal, monkeypatch):
    from plugins.builtin.sakura_mem0 import personal_records
    context, timeline, calls, _ = rehearsal
    original = personal_records.open_personal_memory_from_snapshot
    entered, release = threading.Event(), threading.Event()
    def loading(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(personal_records, 'open_personal_memory_from_snapshot', loading)
    plugin.PersonalWriteRehearsalPlugin().setup(context)
    boundary = context.tools[-1][1].__self__._boundary
    assert entered.wait(5)
    event(context, timeline)
    assert calls == []
    release.set()
    boundary._thread.join(5)
    joined(boundary)
    assert calls == [1]
    assert boundary._curation._curation_state.curation_cursor() == timeline.store.latest_cursor('sakura')


def test_duplicate_event_during_active_job_is_coalesced(rehearsal, monkeypatch):
    context, timeline, calls, client = rehearsal
    original = client.complete_raw
    entered, release = threading.Event(), threading.Event()
    def blocked(self, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(client, 'complete_raw', blocked)
    boundary = start(context)
    event(context, timeline)
    assert entered.wait(5)
    event(context, timeline)
    release.set()
    joined(boundary)
    assert calls == [1]
    assert len(boundary._records.list('sakura')) == 1
    assert not boundary._curation._curation_active


def test_enabled_core_maintainer_updates_profile_after_cursor(rehearsal):
    from datetime import datetime, timezone

    from app.storage.timeline import NewTimelineEntry, TimelineKind
    from plugins.builtin.sakura_mem0.personal_core_profile import read_personal_core_profile

    context, timeline, calls, client = rehearsal
    memory = context.root / "data/memory"
    updated = "2026-09-20T00:00:00+00:00"
    sections = {name: "" for name in ("今の関係", "あなたについて知っていること", "今の私", "大切な約束と境界")}
    (memory / "core_profiles.json").write_text(json.dumps({"sakura": {
        "id": "core_profile:sakura", "schema_version": 2, "content": "", "memory": "",
        "sections": sections, "metadata": {"scope": "sakura", "updated_at": updated, "created_at": updated},
    }}, ensure_ascii=False), encoding="utf-8")
    stamp = datetime.now(timezone.utc).isoformat()
    timeline.store.append_many([
        NewTimelineEntry(entry_id="human-fresh", turn_id="turn-fresh", character_id="sakura",
                         kind=TimelineKind.HUMAN, origin="chat", created_at=stamp,
                         payload={"text": "我们是恋人吧。"}),
        NewTimelineEntry(entry_id="assistant-fresh", turn_id="turn-fresh", character_id="sakura",
                         kind=TimelineKind.ASSISTANT, origin="chat", created_at=stamp,
                         payload={"segments": [{"text": "嗯，是恋人。", "translation": "", "tone": "",
                                                "portrait": "", "suppressTts": False}]}),
    ])
    previous = context.config.get
    context.config.get = lambda: {
        **previous(),
        "coreMaintainer": {"enabled": True},
    }
    prompts = []

    def complete_raw(self, system_prompt, messages, **kwargs):
        prompts.append((system_prompt, messages, kwargs))
        text = messages[0]["content"] if messages else ""
        if "【候補】" not in text:
            return json.dumps({"operations": [
                {"op": "add", "content": "用户喜欢樱花", "layer": "semantic", "confidence": 0.9},
                {"op": "core_candidate", "kind": "explicit", "target_section": "今の関係",
                 "subject_key": "relationship.identity", "claim": "我们明确确认了恋人关系。",
                 "user_excerpt": "我们是恋人吧。", "assistant_excerpt": "嗯，是恋人。",
                 "confidence": 0.95, "batch_id": "forged-batch", "evidence_id": "forged"},
            ]}, ensure_ascii=False)
        candidate_id = text.split("id=", 1)[1].split("\n", 1)[0].strip()
        evidence_id = text.split("- ", 1)[1].split(":", 1)[0].strip()
        assert "Synthetic persona" not in system_prompt
        assert "task" not in kwargs and "thinking" not in kwargs
        return json.dumps({"base_updated_at": updated, "operations": [{
            "op": "replace", "section": "今の関係", "content": "我们明确确认了恋人关系。",
            "reason": "更新认识", "candidate_ids": [candidate_id], "evidence_ids": [evidence_id],
        }]}, ensure_ascii=False)

    client.complete_raw = complete_raw
    boundary = start(context)
    event(context, timeline)
    joined(boundary)
    fragment = read_personal_core_profile(memory, "sakura")
    assert fragment is not None and "我们明确确认了恋人关系。" in fragment["content"]
    assert boundary._curation._curation_state.curation_cursor()
    assert len(boundary._records.list("sakura")) == 1
    assert (memory / "core_review_queue.json").is_file()
    assert len(prompts) == 2


def test_write_plugin_rejects_unadmitted_copy_before_opening(rehearsal):
    context, _, calls, _ = rehearsal
    (context.root / 'data/memory/.personal-write-rehearsal.json').unlink()
    with pytest.raises(ValueError, match='REHEARSAL_REQUIRED'):
        plugin.PersonalWriteRehearsalPlugin().setup(context)
    assert not context.events and not context.tools and calls == []
