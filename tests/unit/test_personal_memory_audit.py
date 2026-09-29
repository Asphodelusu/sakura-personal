import threading

import pytest

from test_personal_memory_backend import dependencies, Encoder
from test_personal_memory_records import fixture_memory
from plugins.builtin.sakura_mem0.personal_records import open_personal_memory


@pytest.mark.parametrize("damage,code", [
    ("none", None), ("source", "SOURCE_REFERENCE"),
    ("entity", "ENTITY_REFERENCE"), ("access", "ACCESS_REFERENCE"),
    ("history", "HISTORY_REFERENCE"), ("foreign_entity", "ENTITY_SCOPE"),
])
def test_audit_distinguishes_valid_deleted_history_and_bad_references(tmp_path, dependencies, damage, code):
    root, identity = fixture_memory(tmp_path, dependencies)
    store = open_personal_memory(root, identity=identity, encoder=Encoder(384))
    try:
        key = store.create("alice", "Sophie reads", metadata={"source_entry_ids": ["entry-1"]})["id"]
        removed = store.create("alice", "removed")["id"]
        store.delete("alice", removed)
        sources = {"alice": {"entry-1"}}
        if damage == "source":
            sources = {"alice": set()}
        if damage == "entity":
            with store._metadata.entities as db:
                db.execute("INSERT INTO entity_memory VALUES('synthetic','absent','now')")
        if damage == "access":
            with store._metadata.access as db:
                db.execute("INSERT INTO memory_access VALUES('absent','now')")
        with store._session.operation() as backend:
            if damage == "history":
                with backend.db.connection as db:
                    db.execute("DELETE FROM history WHERE memory_id=?", (key,))
            if damage == "foreign_entity":
                backend._upsert_entity("synthetic", "ORG", key, {"user_id": "bob"})
        result = store.audit_references(sources)
        assert result["ok"] == (code is None)
        assert result["issues"].get(code, 0) > 0 if code else not result["issues"]
        assert result["counts"]["memories"] == 1
        assert key not in str(result) and "Sophie" not in str(result)
        assert store.get("alice", key) is not None
    finally:
        store.close()


def test_cancelled_audit_is_not_a_success_report(tmp_path, dependencies):
    root, identity = fixture_memory(tmp_path, dependencies)
    store = open_personal_memory(root, identity=identity, encoder=Encoder(384))
    cancel = threading.Event()
    cancel.set()
    try:
        with pytest.raises(RuntimeError, match="AUDIT_CANCELLED"):
            store.audit_references({}, cancel_event=cancel)
        assert not (root / "personal_write_pending.json").exists()
    finally:
        store.close()


def test_audit_reads_all_entity_pages(tmp_path, dependencies):
    from qdrant_client.models import PointStruct
    root, identity = fixture_memory(tmp_path, dependencies)
    store = open_personal_memory(root, identity=identity, encoder=Encoder(384))
    try:
        key = store.create("alice", "original")["id"]
        with store._session.operation() as backend:
            backend.vector_store.client.upsert("sakura_memories_entities", points=[
                PointStruct(id=n, vector=[1.] + [0.] * 383,
                            payload={"user_id": "alice", "linked_memory_ids": [key if n < 129 else "absent"]})
                for n in range(130)
            ], wait=True)
        result = store.audit_references({})
        assert not result["ok"]
        assert result["counts"]["vector_entities"] == 130
        assert result["issues"] == {"ENTITY_REFERENCE": 1}
    finally:
        store.close()


def test_audit_cancellation_during_scan_does_not_return_partial_counts(tmp_path, dependencies, monkeypatch):
    root, identity = fixture_memory(tmp_path, dependencies)
    store = open_personal_memory(root, identity=identity, encoder=Encoder(384))
    cancel = threading.Event()
    try:
        key = store.create("alice", "original")["id"]
        with store._session.operation() as backend:
            original = backend.vector_store.client.scroll
            def interrupted(*args, **kwargs):
                result = original(*args, **kwargs)
                cancel.set()
                return result
            monkeypatch.setattr(backend.vector_store.client, "scroll", interrupted)
        with pytest.raises(RuntimeError, match="AUDIT_CANCELLED"):
            store.audit_references({}, cancel_event=cancel)
        assert store.get("alice", key)["memory"] == "original"
    finally:
        store.close()


def test_audit_cannot_clear_pending_write_even_when_references_look_valid(tmp_path, dependencies):
    root, identity = fixture_memory(tmp_path, dependencies)
    store = open_personal_memory(root, identity=identity, encoder=Encoder(384))
    marker = root / "personal_write_pending.json"
    try:
        store.create("alice", "original")
        marker.write_text('{"schema":1,"operation":"update"}', encoding="utf-8")
        before = marker.read_bytes()
        with pytest.raises(RuntimeError, match="WRITE_INCOMPLETE"):
            store.audit_references({})
        assert marker.read_bytes() == before
    finally:
        store.close()
