import pytest

from app.storage.timeline import TimelineStore, NewTimelineEntry, TimelineKind
from test_personal_memory_backend import dependencies, Encoder
from test_personal_memory_records import fixture_memory
from plugins.builtin.sakura_mem0.personal_records import open_personal_memory


def test_sqlite_mapping_audit_distinguishes_legacy_evidence_from_verified_references(tmp_path, dependencies):
    from app.legacy_import.history import import_history
    from app.legacy_import.history_sqlite import read_personal_history_rows
    from test_personal_sqlite_history import fixture_source

    source, _ = fixture_source(tmp_path)
    root, identity = fixture_memory(tmp_path, dependencies)
    # Keep the converted Timeline in the same isolated data parent as Memory.
    converted = tmp_path / "converted"
    import_history(source, converted, character_ids=("alice",))
    path = tmp_path / "chat_history/timeline.sqlite3"
    path.parent.mkdir()
    path.write_bytes((converted / "data/chat_history/timeline.sqlite3").read_bytes())
    mapping = read_personal_history_rows(path)
    assistant_id = next(row[3] for row in mapping if row[2] == 20)
    store = open_personal_memory(root, identity=identity, encoder=Encoder(384))
    try:
        legacy = store.create("alice", "legacy fact", metadata={
            "source": "self_curation", "evidence": "first", "event_time": "old time",
        })
        store.create("alice", "mapped fact", metadata={"source_entry_ids": [assistant_id]})
        store.create("alice", "empty references", metadata={"source_entry_ids": []})
        before = store.get("alice", legacy["id"])
        result = store.audit_timeline(path)
        assert result["ok"]
        assert result["counts"]["memories_without_source_references"] == 2
        assert result["counts"]["verified_source_references"] == 1
        assert store.get("alice", legacy["id"]) == before
        assert "source_entry_ids" not in before["metadata"]
        assert "first" not in str(result)
        store.create("bob", "foreign", metadata={"source_entry_ids": [assistant_id]})
        store.create("alice", "missing", metadata={"source_entry_ids": ["not-found"]})
        result = store.audit_timeline(path)
        assert result["issues"] == {"SOURCE_REFERENCE": 2}
        assert result["counts"]["verified_source_references"] == 1
    finally:
        store.close()


def test_real_timeline_schema_drives_scoped_reference_audit_without_reading_text(tmp_path, dependencies):
    root, identity = fixture_memory(tmp_path, dependencies)
    path = tmp_path / "chat_history/timeline.sqlite3"
    timeline = TimelineStore(path)
    timeline.initialize()
    timeline.append(NewTimelineEntry(entry_id="entry-1", turn_id="turn-1", character_id="alice",
        kind=TimelineKind.HUMAN, origin="chat", created_at="2026-09-14T00:00:00+00:00", payload={"text": "private synthetic text"}))
    store = open_personal_memory(root, identity=identity, encoder=Encoder(384))
    before = path.read_bytes()
    try:
        store.create("alice", "fact", metadata={"source_entry_ids": ["entry-1"]})
        assert store.audit_timeline(path)["ok"]
        store.create("bob", "foreign", metadata={"source_entry_ids": ["entry-1"]})
        result = store.audit_timeline(path)
        assert result["issues"] == {"SOURCE_REFERENCE": 1}
        assert "private synthetic text" not in str(result)
    finally:
        store.close()
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["missing", "corrupt", "escape"])
def test_invalid_timeline_cannot_look_like_an_empty_valid_source_map(tmp_path, dependencies, kind):
    root, identity = fixture_memory(tmp_path, dependencies)
    path = tmp_path / "timeline.sqlite3"
    if kind == "corrupt":
        path.write_bytes(b"corrupt")
    elif kind == "escape":
        path = tmp_path.parent / "not-owned.sqlite3"
    store = open_personal_memory(root, identity=identity, encoder=Encoder(384))
    try:
        with pytest.raises(ValueError):
            store.audit_timeline(path)
    finally:
        store.close()
    if kind == "missing":
        assert not path.exists()
