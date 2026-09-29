import json
import os
import threading

import pytest

from app.legacy_import import memory_contract
from app.legacy_import.personal_copy import prepare_personal_copy, STATE_FILE
from app.storage.timeline import TimelineStore, NewTimelineEntry, TimelineKind
from plugins.builtin.sakura_mem0.personal_records import open_personal_memory
from test_personal_memory_backend import dependencies, Encoder
from test_personal_memory_records import fixture_memory


class TrackedEncoder(Encoder):
    closes = 0
    def close(self):
        self.closes += 1
        super().close()


def fixture_copy(tmp_path, dependencies, dimensions=384):
    source = tmp_path / "source"
    root, identity = fixture_memory(source / "data", dependencies, dimensions)
    timeline = TimelineStore(source / "data/chat_history/timeline.sqlite3")
    timeline.initialize()
    timeline.append(NewTimelineEntry(entry_id="entry", turn_id="turn", character_id="alice",
        kind=TimelineKind.HUMAN, origin="chat", created_at="2026-09-17T00:00:00+00:00",
        payload={"text": "private synthetic text"}))
    store = open_personal_memory(root, identity=identity, encoder=Encoder(dimensions))
    try:
        store.create("alice", "private synthetic fact", metadata={"evidence": "private synthetic text"})
    finally:
        store.close()
    work = tmp_path / "work"
    prepare_personal_copy(source, work, source_is_quiescent=lambda: True)
    return source, work, identity


@pytest.mark.parametrize("dimensions", [384, 1024])
def test_inspection_connects_copy_timeline_and_personal_backend_and_releases_resources(tmp_path, dependencies, dimensions):
    source, work, identity = fixture_copy(tmp_path, dependencies, dimensions)
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    for _ in range(2):
        encoder = TrackedEncoder(dimensions)
        result = memory_contract.inspect_personal_migration(work, identity=identity, encoder=encoder)
        assert result["ok"]
        assert result["counts"]["memories"] == 1
        assert result["counts"]["memories_without_source_references"] == 1
        assert encoder.closes == 1
        assert "private synthetic" not in str(result)
    assert before == {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}


@pytest.mark.parametrize("damage", ["no_marker", "incomplete", "timeline", "identity", "cancel", "reference", "hardlink", "pending", "metadata"])
def test_failed_inspection_never_claims_success_and_closes_encoder(tmp_path, dependencies, damage):
    _, work, identity = fixture_copy(tmp_path, dependencies)
    cancel = threading.Event()
    if damage == "no_marker":
        (work / STATE_FILE).unlink()
    elif damage == "incomplete":
        (work / STATE_FILE).write_text(json.dumps({"state": "incomplete"}))
    elif damage == "timeline":
        (work / "data/chat_history/timeline.sqlite3").unlink()
    elif damage == "identity":
        identity = {**identity, "artifact_sha256": "0" * 64}
    elif damage == "cancel":
        cancel.set()
    elif damage == "hardlink":
        os.link(work / "data/memory/active_index.json", work / "linked.json")
    elif damage == "pending":
        (work / "data/memory/personal_write_pending.json").write_text("{}")
    elif damage == "metadata":
        (work / "data/memory/entity_index.db").unlink()
    elif damage == "reference":
        store = open_personal_memory(work / "data/memory", identity=identity, encoder=Encoder(384))
        try:
            store.create("bob", "bad", metadata={"source_entry_ids": ["entry"]})
        finally:
            store.close()
    encoder = TrackedEncoder(384)
    if damage == "reference":
        result = memory_contract.inspect_personal_migration(work, identity=identity, encoder=encoder)
        assert not result["ok"] and result["issues"] == {"SOURCE_REFERENCE": 1}
    else:
        with pytest.raises((ValueError, RuntimeError)):
            memory_contract.inspect_personal_migration(work, identity=identity, encoder=encoder, cancel_event=cancel)
    assert encoder.closes == 1
