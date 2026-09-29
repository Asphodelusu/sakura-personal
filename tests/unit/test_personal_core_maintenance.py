"""Personal core-profile maintenance: curator candidates, queue, and V2 writes."""
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plugins.builtin.sakura_mem0.domain_types import ChatHistoryEntry
from plugins.builtin.sakura_mem0.memory_curator import MemoryCurator
from plugins.builtin.sakura_mem0.personal_core_profile import (
    CoreProfileStorageError,
    read_personal_core_profile,
)
from plugins.builtin.sakura_mem0.personal_profile_maintenance import (
    ProfileMaintenance,
    commit_curation_then_maintain,
)


NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
UPDATED = "2026-09-20T00:00:00+00:00"
SECTION = "今の関係"
SECTIONS = ("今の関係", "あなたについて知っていること", "今の私", "大切な約束と境界")


def test_legacy_naive_timestamps_keep_local_timezone(monkeypatch):
    from plugins.builtin.sakura_mem0 import support

    local = timezone(timedelta(hours=8))

    class LocalDateTime(datetime):
        def astimezone(self, tz=None):
            if self.tzinfo is None:
                return self.replace(tzinfo=local).astimezone(tz or local)
            return super().astimezone(tz or local)

    monkeypatch.setattr(support, "datetime", LocalDateTime)
    assert support.parse_iso_datetime("2026-09-27T09:00:00").utcoffset() == timedelta(hours=8)
    assert support.parse_iso_datetime("2026-09-27T09:00:00-03:00").utcoffset() == timedelta(hours=-3)
    assert support.parse_iso_datetime("2026-09-27T09:00:00Z").utcoffset() == timedelta(0)


def test_close_preserves_another_owners_lease(tmp_path):
    memory = _memory(tmp_path)
    maintenance = _maintenance(memory)
    state = maintenance._state_store()
    assert state.try_acquire_lease("other-owner")
    path = memory / "core_maintainer_state.json"
    before = path.read_bytes()
    maintenance.close()
    assert path.read_bytes() == before


@pytest.mark.parametrize("linked_name", ["core_profiles.json", "core_profiles.json.bak", "core_profiles.json.lock"])
def test_profile_patch_refuses_linked_file_and_backup(tmp_path, linked_name):
    from plugins.builtin.sakura_mem0.personal_core_profile import patch_personal_core_profile_sections

    memory = _memory(tmp_path)
    profile = _profile(memory)
    outside = tmp_path / "outside-profile.json"
    outside.write_bytes(profile.read_bytes())
    linked = memory / linked_name
    linked.unlink(missing_ok=True)
    os.link(outside, linked)
    before = {path.name: path.read_bytes() for path in memory.iterdir()}
    original = outside.read_bytes()
    with pytest.raises(CoreProfileStorageError):
        patch_personal_core_profile_sections(memory, "alice", UPDATED, {SECTION: "changed"})
    assert outside.read_bytes() == original
    assert {path.name: path.read_bytes() for path in memory.iterdir()} == before


def test_close_while_profile_write_waits_for_lock_discards_result(tmp_path, monkeypatch):
    from plugins.builtin.sakura_mem0 import personal_core_profile as profiles

    memory = _memory(tmp_path)
    path = _profile(memory)
    before = path.read_bytes()
    maintenance = _maintenance(memory)
    result, _, _ = _curate([_candidate()])
    arrived = threading.Event()
    real_lock = profiles._exclusive_profile_lock
    errors = []

    @contextmanager
    def observed_lock(target):
        if threading.current_thread() is thread:
            arrived.set()
        with real_lock(target):
            yield

    def complete_raw(*args, **kwargs):
        queue = json.loads((memory / "core_review_queue.json").read_text(encoding="utf-8"))
        item = queue["scopes"]["alice"]["candidates"][0]
        return _proposal(item["id"], item["evidence"][0]["id"])

    def work():
        try:
            commit_curation_then_maintain(
                maintenance, _entries(), result.core_candidates,
                mark_success=lambda: None, completion=Completion(complete_raw),
            )
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=work)
    closer = threading.Thread(target=maintenance.close)
    monkeypatch.setattr(profiles, "_exclusive_profile_lock", observed_lock)
    try:
        with real_lock(path):
            thread.start()
            assert arrived.wait(5)
            closer.start()
            assert maintenance._cancel.wait(5)
    finally:
        thread.join(5)
        if closer.ident is not None:
            closer.join(5)
    assert not thread.is_alive()
    assert not closer.is_alive()
    assert errors == []
    assert path.read_bytes() == before


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class VectorStore:
    def __init__(self):
        self.created = []
        self.allow_profile_candidates = True
        self.scope_id = "alice"

    def list_memories(self, *, limit=None):
        return []

    def create_memory(self, arguments, *, allow_sensitive=False):
        self.created.append(dict(arguments))
        return {"ok": True}

    def update_memory(self, arguments, *, allow_sensitive=False):
        return {}

    def delete_memory(self, arguments):
        return {}


class Completion:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def complete_raw(self, system_prompt, messages, **kwargs):
        self.calls.append({"system": system_prompt, "messages": messages, "kwargs": kwargs})
        if isinstance(self.response, BaseException):
            raise self.response
        if callable(self.response):
            return self.response(system_prompt, messages, **kwargs)
        return self.response


def _memory(tmp_path: Path) -> Path:
    memory = tmp_path / "memory"
    memory.mkdir()
    root = str(memory.resolve())
    (memory / ".personal-write-rehearsal.json").write_text(
        json.dumps({"purpose": "personal-memory-write-rehearsal", "root": root}),
        encoding="utf-8",
    )
    (memory / ".sakura-personal-copy.json").write_text(
        json.dumps({"state": "complete"}),
        encoding="utf-8",
    )
    return memory


def _profile(memory: Path, *, schema=2, sections=None, updated=UPDATED, extra=None) -> Path:
    body = {name: "" for name in SECTIONS}
    if sections:
        body.update(sections)
    record = {
        "id": "core_profile:alice",
        "schema_version": schema,
        "content": "",
        "memory": "",
        "sections": body,
        "metadata": {"scope": "alice", "updated_at": updated, "created_at": updated},
    }
    if extra:
        record.update(extra)
    payload = {"alice": record, "bob": {"schema_version": 2, "content": "bob stays", "sections": dict(body),
                                        "metadata": {"scope": "bob", "updated_at": updated}}}
    path = memory / "core_profiles.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _entries():
    return [
        ChatHistoryEntry(created_at="2026-09-27T11:00:00+00:00", role="user",
                         content="我们是恋人吧。", entry_id="human-1", turn_id="turn-1"),
        ChatHistoryEntry(created_at="2026-09-27T11:00:01+00:00", role="assistant",
                         content="嗯，是恋人。", entry_id="assistant-1", turn_id="turn-1"),
    ]


def _candidate(**overrides):
    payload = {
        "op": "core_candidate",
        "kind": "explicit",
        "target_section": SECTION,
        "subject_key": "relationship.identity",
        "claim": "我们明确确认了恋人关系。",
        "user_excerpt": "我们是恋人吧。",
        "assistant_excerpt": "嗯，是恋人。",
        "confidence": 0.95,
        "batch_id": "model-batch",
        "observed_at": "2000-01-01T00:00:00+00:00",
        "evidence_id": "forged",
        "scope": "alice",
    }
    payload.update(overrides)
    return payload


def _proposal(candidate_id, evidence_id, *, base=UPDATED, operations=None):
    if operations is None:
        operations = [{
            "op": "replace",
            "section": SECTION,
            "content": "我们明确确认了恋人关系。",
            "reason": "更新认识",
            "candidate_ids": [candidate_id],
            "evidence_ids": [evidence_id],
        }]
    return json.dumps({"base_updated_at": base, "operations": operations}, ensure_ascii=False)


def _enabled():
    return {"enabled": True, "normal_cooldown_hours": 6}


def _maintenance(memory, settings=_enabled(), clock=None):
    return ProfileMaintenance(memory, "alice", settings=settings, clock=clock or Clock())


def _curate(operations, *, accept=True):
    store = VectorStore()
    client = Completion(json.dumps({"operations": operations}, ensure_ascii=False))
    curator = MemoryCurator(client, store, accept_core_candidates=accept)
    result = curator.curate_entries(_entries())
    return result, store, client


def test_default_curator_does_not_accept_core_candidates():
    result, store, client = _curate([_candidate(), {"op": "add", "content": "用户喜欢茶", "layer": "semantic"}],
                                    accept=False)
    assert result.core_candidates == ()
    assert store.created and store.created[0]["layer"] == "semantic"
    assert "core_candidate" not in client.calls[0]["system"]


def test_grounded_candidate_is_separated_from_vector_writes():
    result, store, client = _curate([
        _candidate(),
        _candidate(user_excerpt="不存在的原话", assistant_excerpt="也不存在"),
        _candidate(scope="bob"),
        {"op": "add", "content": "用户喜欢茶", "layer": "semantic", "confidence": 0.9},
    ])
    assert len(result.core_candidates) == 1
    assert store.created[0]["content"] == "用户喜欢茶"
    assert "core_candidate" in client.calls[0]["system"]
    assert "forged" not in json.dumps(result.core_candidates[0])


def test_curator_caps_five_candidates_per_job():
    operations = [
        _candidate(subject_key=f"relationship.identity.{index}", claim=f"认识{index}")
        for index in range(6)
    ]
    result, _, _ = _curate(operations)
    assert len(result.core_candidates) == 5


def test_queue_persists_before_cursor_and_maintenance_updates_context(tmp_path):
    memory = _memory(tmp_path)
    path = _profile(memory)
    result, _, _ = _curate([_candidate()])
    order = []
    completion = Completion("")

    def mark_success():
        order.append("cursor")
        assert (memory / "core_review_queue.json").is_file()

    def complete_raw(system_prompt, messages, **kwargs):
        order.append("model")
        assert "我们是恋人吧。" in messages[0]["content"]
        assert "Synthetic persona" not in system_prompt
        assert "task" not in kwargs and "thinking" not in kwargs
        stored = json.loads((memory / "core_review_queue.json").read_text(encoding="utf-8"))
        item = stored["scopes"]["alice"]["candidates"][0]
        assert item["evidence"][0]["id"] != "forged"
        assert item["evidence"][0]["batch_id"] == "turn-1"
        assert item["evidence"][0]["observed_at"] == "2026-09-27T11:00:00+00:00"
        completion.calls.append(1)
        return _proposal(item["id"], item["evidence"][0]["id"])

    completion.complete_raw = complete_raw
    commit_curation_then_maintain(
        _maintenance(memory), _entries(), result.core_candidates,
        mark_success=mark_success, completion=completion,
    )
    assert order == ["cursor", "model"]
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["alice"]["sections"][SECTION] == "我们明确确认了恋人关系。"
    assert saved["bob"]["content"] == "bob stays"
    fragment = read_personal_core_profile(memory, "alice")
    assert "我们明确确认了恋人关系。" in fragment["content"]
    assert fragment["sensitivity"] == "private"


def test_candidate_failure_keeps_cursor_and_corrupt_queue(tmp_path):
    memory = _memory(tmp_path)
    queue = memory / "core_review_queue.json"
    queue.write_text("{", encoding="utf-8")
    result, store, _ = _curate([_candidate(), {"op": "add", "content": "用户喜欢茶", "layer": "semantic", "confidence": 0.9}])
    marked = []
    with pytest.raises(Exception):
        commit_curation_then_maintain(
            _maintenance(memory), _entries(), result.core_candidates,
            mark_success=lambda: marked.append(1), completion=Completion("{}"),
        )
    assert marked == []
    assert queue.read_text(encoding="utf-8") == "{"
    assert store.created


def test_absent_and_disabled_and_recall_only_do_not_write(tmp_path):
    memory = _memory(tmp_path)
    path = _profile(memory)
    before = path.read_bytes()
    result, _, _ = _curate([_candidate()])
    for settings in (None, {"enabled": False}):
        marked = []
        called = []

        def refuse(*_args, **_kwargs):
            called.append(1)
            raise AssertionError("maintenance model")

        commit_curation_then_maintain(
            ProfileMaintenance(memory, "alice", settings=settings, clock=Clock()),
            _entries(), result.core_candidates,
            mark_success=lambda: marked.append(1),
            completion=Completion(refuse),
        )
        assert marked == [1]
        assert called == []
        assert not (memory / "core_review_queue.json").exists()
        assert not (memory / "core_maintainer_state.json").exists()
    assert path.read_bytes() == before
    bare = tmp_path / "recall"
    bare.mkdir()
    profile = _profile(bare)
    original = profile.read_bytes()
    with pytest.raises(ValueError, match="PERSONAL_WRITE_REHEARSAL_REQUIRED"):
        from plugins.builtin.sakura_mem0.personal_core_profile import patch_personal_core_profile_sections
        patch_personal_core_profile_sections(
            bare, "alice", UPDATED, {SECTION: "不应该写入"},
        )
    assert profile.read_bytes() == original


def test_model_failure_keeps_successful_cursor_and_profile(tmp_path):
    memory = _memory(tmp_path)
    path = _profile(memory)
    before = path.read_bytes()
    result, _, _ = _curate([_candidate()])
    marked = []
    commit_curation_then_maintain(
        _maintenance(memory), _entries(), result.core_candidates,
        mark_success=lambda: marked.append(1),
        completion=Completion(RuntimeError("synthetic model failure")),
    )
    assert marked == [1]
    assert path.read_bytes() == before
    assert (memory / "core_review_queue.json").is_file()


def test_stale_revision_unknown_schema_and_too_many_sections_keep_files(tmp_path):
    from plugins.builtin.sakura_mem0.personal_core_profile import patch_personal_core_profile_sections

    memory = _memory(tmp_path)
    path = _profile(memory)
    backup = path.with_name(path.name + ".bak")
    backup.write_bytes(b"sentinel-backup")
    original = path.read_bytes()
    with pytest.raises(CoreProfileStorageError):
        patch_personal_core_profile_sections(
            memory, "alice", "1999-01-01T00:00:00+00:00", {SECTION: "新正文"},
        )
    with pytest.raises(CoreProfileStorageError):
        patch_personal_core_profile_sections(
            memory, "alice", UPDATED,
            {SECTIONS[0]: "一", SECTIONS[1]: "二", SECTIONS[2]: "三"},
        )
    assert path.read_bytes() == original
    assert backup.read_bytes() == b"sentinel-backup"
    unknown = _profile(memory, schema=3, extra={"content": "kept text"})
    unknown_before = unknown.read_bytes()
    with pytest.raises(CoreProfileStorageError):
        patch_personal_core_profile_sections(memory, "alice", UPDATED, {SECTION: "新正文"})
    assert unknown.read_bytes() == unknown_before
    assert backup.read_bytes() == b"sentinel-backup"


def test_corrupt_maintainer_state_is_not_overwritten(tmp_path):
    memory = _memory(tmp_path)
    _profile(memory)
    state = memory / "core_maintainer_state.json"
    state.write_text("{", encoding="utf-8")
    result, _, _ = _curate([_candidate()])
    marked = []
    commit_curation_then_maintain(
        _maintenance(memory), _entries(), result.core_candidates,
        mark_success=lambda: marked.append(1), completion=Completion("{}"),
    )
    assert marked == [1]
    assert state.read_text(encoding="utf-8") == "{"


def test_partial_queue_repair_does_not_patch_twice(tmp_path, monkeypatch):
    memory = _memory(tmp_path)
    path = _profile(memory)
    result, _, _ = _curate([_candidate()])
    from plugins.builtin.sakura_mem0 import personal_core_candidates as candidates

    original = candidates.CoreCandidateQueue.mark_processed
    attempts = {"n": 0}

    def flaky(self, *args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("synthetic queue failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(candidates.CoreCandidateQueue, "mark_processed", flaky)
    calls = {"n": 0}

    def complete_raw(system_prompt, messages, **kwargs):
        calls["n"] += 1
        stored = json.loads((memory / "core_review_queue.json").read_text(encoding="utf-8"))
        item = stored["scopes"]["alice"]["candidates"][0]
        return _proposal(item["id"], item["evidence"][0]["id"])

    maintenance = _maintenance(memory)
    commit_curation_then_maintain(
        maintenance, _entries(), result.core_candidates,
        mark_success=lambda: None, completion=Completion(complete_raw),
    )
    once = json.loads(path.read_text(encoding="utf-8"))["alice"]["metadata"]["updated_at"]
    commit_curation_then_maintain(
        maintenance, _entries(), (),
        mark_success=lambda: None, completion=Completion(complete_raw),
    )
    twice = json.loads(path.read_text(encoding="utf-8"))
    assert calls["n"] == 1
    assert twice["alice"]["metadata"]["updated_at"] == once
    assert twice["alice"]["sections"][SECTION] == "我们明确确认了恋人关系。"
    stored = json.loads((memory / "core_review_queue.json").read_text(encoding="utf-8"))
    assert stored["scopes"]["alice"]["candidates"][0]["status"] == "applied"


def test_duplicate_delivery_does_not_reapply(tmp_path):
    memory = _memory(tmp_path)
    path = _profile(memory)
    result, _, _ = _curate([_candidate()])
    calls = {"n": 0}

    def complete_raw(system_prompt, messages, **kwargs):
        calls["n"] += 1
        stored = json.loads((memory / "core_review_queue.json").read_text(encoding="utf-8"))
        item = stored["scopes"]["alice"]["candidates"][0]
        return _proposal(item["id"], item["evidence"][0]["id"])

    maintenance = _maintenance(memory)
    for _ in range(2):
        commit_curation_then_maintain(
            maintenance, _entries(), result.core_candidates,
            mark_success=lambda: None, completion=Completion(complete_raw),
        )
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert calls["n"] == 1
    assert saved["alice"]["sections"][SECTION].count("恋人关系") == 1


def test_close_discards_late_maintenance_result(tmp_path):
    memory = _memory(tmp_path)
    path = _profile(memory)
    before = path.read_bytes()
    result, _, _ = _curate([_candidate()])
    maintenance = _maintenance(memory)
    entered, release = threading.Event(), threading.Event()

    def complete_raw(system_prompt, messages, **kwargs):
        entered.set()
        assert release.wait(5)
        stored = json.loads((memory / "core_review_queue.json").read_text(encoding="utf-8"))
        item = stored["scopes"]["alice"]["candidates"][0]
        return _proposal(item["id"], item["evidence"][0]["id"])

    def worker():
        commit_curation_then_maintain(
            maintenance, _entries(), result.core_candidates,
            mark_success=lambda: None, completion=Completion(complete_raw),
        )

    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(5)
    maintenance.close()
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert path.read_bytes() == before
    state = json.loads((memory / "core_maintainer_state.json").read_text(encoding="utf-8"))
    assert state.get("lease") in (None, {})


def test_legacy_migration_keeps_sentences_numbers_and_quotes(tmp_path):
    from plugins.builtin.sakura_mem0.personal_core_profile import patch_personal_core_profile_sections

    memory = _memory(tmp_path)
    legacy = "我们约好每周见面。「咖啡」要 2 杯。"
    path = memory / "core_profiles.json"
    path.write_text(json.dumps({"alice": {
        "id": "core_profile:alice",
        "schema_version": 2,
        "content": legacy,
        "memory": legacy,
        "sections": {"legacy": legacy},
        "metadata": {"scope": "alice", "updated_at": UPDATED, "created_at": UPDATED},
    }}, ensure_ascii=False), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(CoreProfileStorageError):
        patch_personal_core_profile_sections(memory, "alice", UPDATED, {
            "今の関係": "我们约好每周见面。",
            "あなたについて知っていること": "「咖啡」要喝。",
            "今の私": "",
            "大切な約束と境界": "",
        }, migrate_legacy=True)
    assert path.read_bytes() == before
    patch_personal_core_profile_sections(memory, "alice", UPDATED, {
        "今の関係": "我们约好每周见面。",
        "あなたについて知っていること": "「咖啡」要 2 杯。",
        "今の私": "",
        "大切な約束と境界": "",
    }, migrate_legacy=True)
    saved = json.loads(path.read_text(encoding="utf-8"))["alice"]
    assert "legacy" not in saved["sections"]
    assert saved["sections"]["あなたについて知っていること"] == "「咖啡」要 2 杯。"


def test_concurrent_scope_patches_keep_both_records(tmp_path):
    from plugins.builtin.sakura_mem0.personal_core_profile import patch_personal_core_profile_sections

    memory = _memory(tmp_path)
    _profile(memory)
    barrier = threading.Barrier(2)
    errors = []

    def patch(scope, text):
        try:
            barrier.wait(5)
            patch_personal_core_profile_sections(memory, scope, UPDATED, {SECTION: text})
        except Exception as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [
        threading.Thread(target=patch, args=("alice", "甲的关系")),
        threading.Thread(target=patch, args=("bob", "乙的关系")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert errors == []
    saved = json.loads((memory / "core_profiles.json").read_text(encoding="utf-8"))
    assert saved["alice"]["sections"][SECTION] == "甲的关系"
    assert saved["bob"]["sections"][SECTION] == "乙的关系"
