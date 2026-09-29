import sqlite3
import sys
import threading
from contextlib import closing
from pathlib import Path

import pytest

from test_personal_memory_backend import dependencies, fixture_index, Encoder
from plugins.builtin.sakura_mem0 import personal_records as records


@pytest.mark.skipif(sys.platform != "win32", reason="Windows verbatim paths")
def test_desktop_verbatim_path_opens_existing_personal_databases(tmp_path, dependencies):
    root, identity = fixture_memory(tmp_path / "memory space #", dependencies)
    extended = Path("\\\\?\\" + str(root))
    store = records.open_personal_memory(extended, identity=identity, encoder=Encoder(384))
    try:
        key = store.create("alice", "synthetic desktop fact")["id"]
        assert store.get("alice", key)["memory"] == "synthetic desktop fact"
        store.record_access("alice", [key], when="2026-09-20T00:00:00+00:00")
        assert store.last_accessed("alice", [key])[key] == "2026-09-20T00:00:00+00:00"
    finally:
        store.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows verbatim paths")
def test_verbatim_sqlite_uri_preserves_read_only_and_existing_file_guards(tmp_path):
    from plugins.builtin.sakura_mem0.personal_backend import _sqlite_uri
    path = tmp_path / "metadata #.db"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE fixture (value TEXT)")
    extended = Path("\\\\?\\" + str(path))
    with closing(sqlite3.connect(_sqlite_uri(extended) + "?mode=ro", uri=True)) as db:
        assert db.execute("SELECT * FROM fixture").fetchall() == []
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("INSERT INTO fixture VALUES ('must not write')")
    missing = extended.with_name("must not create.db")
    for mode in ("ro", "rw"):
        with pytest.raises(sqlite3.OperationalError):
            sqlite3.connect(_sqlite_uri(missing) + "?mode=" + mode, uri=True)
    assert not missing.exists()


def fixture_memory(tmp_path, dependencies, dimensions=384, *, legacy=False):
    root, identity = fixture_index(tmp_path, dimensions, dependencies, legacy=legacy)
    with closing(sqlite3.connect(root / "entity_index.db")) as db:
        db.executescript("""
            CREATE TABLE entity_memory(entity TEXT NOT NULL, memory_id TEXT NOT NULL,
              updated_at TEXT NOT NULL, PRIMARY KEY(entity, memory_id));
            CREATE TABLE _meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO _meta VALUES('backfilled','1');
            INSERT INTO _meta VALUES('unknown','keep');
        """)
    with closing(sqlite3.connect(root / "access_tracker.db")) as db:
        db.executescript("""
            CREATE TABLE memory_access(memory_id TEXT PRIMARY KEY, last_accessed TEXT NOT NULL);
            CREATE TABLE _meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO _meta VALUES('migrated_from_json','1');
        """)
    return root, identity


@pytest.mark.parametrize("dimensions", [384, 1024])
def test_scoped_update_preserves_metadata_aliases_access_and_restart(tmp_path, dependencies, dimensions):
    root, identity = fixture_memory(tmp_path, dependencies, dimensions)
    store = records.open_personal_memory(root, identity=identity, encoder=Encoder(dimensions))
    try:
        first = store.create("alice", "ソフィア likes books", metadata={"source_entry_ids": ["entry-1"], "personal": {"keep": 1}})
        key = first["id"]
        store.record_access("alice", [key], when="2026-09-14T00:00:00+00:00")
        assert store.lookup_entities("alice", ["索菲"]) == [key]
        assert store.lookup_entities("bob", ["索菲"]) == []
        store.update("alice", key, "Alice likes books")
        assert store.lookup_entities("alice", ["索菲"]) == []
        assert store.lookup_entities("alice", ["Alice"]) == [key]
    finally:
        store.close()
    store = records.open_personal_memory(root, identity=identity, encoder=Encoder(dimensions))
    try:
        item = store.get("alice", key)
        assert item["metadata"]["source_entry_ids"] == ["entry-1"]
        assert item["metadata"]["personal"] == {"keep": 1}
        assert store.last_accessed("alice", [key]) == {key: "2026-09-14T00:00:00+00:00"}
        store.delete("alice", key)
        assert store.get("alice", key) is None
        with closing(sqlite3.connect(root / "entity_index.db")) as db:
            assert db.execute("SELECT * FROM entity_memory").fetchall() == []
            assert db.execute("SELECT value FROM _meta WHERE key='unknown'").fetchone() == ("keep",)
        with closing(sqlite3.connect(root / "access_tracker.db")) as db:
            assert db.execute("SELECT * FROM memory_access").fetchall() == []
    finally:
        store.close()


def test_foreign_scope_cannot_get_modify_delete_or_mark_access(tmp_path, dependencies):
    root, identity = fixture_memory(tmp_path, dependencies)
    store = records.open_personal_memory(root, identity=identity, encoder=Encoder(384))
    try:
        key = store.create("alice", "original")["id"]
        assert store.get("bob", key) is None
        for operation in (lambda: store.update("bob", key, "changed"),
                          lambda: store.delete("bob", key),
                          lambda: store.record_access("bob", [key], when="now")):
            with pytest.raises(ValueError, match="SCOPE"):
                operation()
        assert store.get("alice", key)["memory"] == "original"
        assert not (root / "personal_write_pending.json").exists()
    finally:
        store.close()


def test_partial_metadata_failure_blocks_current_and_reopened_store(tmp_path, dependencies, monkeypatch):
    root, identity = fixture_memory(tmp_path, dependencies)
    store = records.open_personal_memory(root, identity=identity, encoder=Encoder(384))
    try:
        key = store.create("alice", "original")["id"]
        monkeypatch.setattr(store._metadata, "replace_entities", lambda *a, **k: (_ for _ in ()).throw(OSError("disk failure")))
        with pytest.raises(OSError, match="disk failure"):
            store.update("alice", key, "partial update")
        with pytest.raises(RuntimeError, match="INCOMPLETE"):
            store.get("alice", key)
    finally:
        store.close()
    assert (root / "personal_write_pending.json").exists()
    with pytest.raises(RuntimeError, match="INCOMPLETE"):
        records.open_personal_memory(root, identity=identity, encoder=Encoder(384))


@pytest.mark.parametrize("name", ["entity_index.db", "access_tracker.db"])
def test_missing_associated_store_never_becomes_a_new_empty_database(tmp_path, dependencies, name):
    root, identity = fixture_memory(tmp_path, dependencies)
    (root / name).unlink()
    with pytest.raises(ValueError, match="METADATA"):
        records.open_personal_memory(root, identity=identity, encoder=Encoder(384))
    assert not (root / name).exists()


def test_delete_after_restart_removes_vector_links_without_lazy_entity_initialization(tmp_path, dependencies):
    root, identity = fixture_memory(tmp_path, dependencies)
    store = records.open_personal_memory(root, identity=identity, encoder=Encoder(384))
    key = store.create("alice", "original")["id"]
    kept = store.create("alice", "retained")["id"]
    with store._session.operation() as backend:
        backend._upsert_entity("shared", "ORG", key, {"user_id": "alice"})
        backend._upsert_entity("shared", "ORG", kept, {"user_id": "alice"})
    store.close()
    store = records.open_personal_memory(root, identity=identity, encoder=Encoder(384))
    try:
        store.delete("alice", key)
        with store._session.operation() as backend:
            rows, _ = backend.vector_store.client.scroll("sakura_memories_entities", with_payload=True)
            assert len(rows) == 1
            assert rows[0].payload["linked_memory_ids"] == [kept]
    finally:
        store.close()


def test_close_waits_for_real_metadata_transaction(tmp_path, dependencies, monkeypatch):
    root, identity = fixture_memory(tmp_path, dependencies)
    store = records.open_personal_memory(root, identity=identity, encoder=Encoder(384))
    reached, finish, close_requested, closed = (threading.Event() for _ in range(4))
    original = store._metadata.replace_entities
    errors, created = [], []
    def paused(*args):
        reached.set()
        assert finish.wait(5)
        original(*args)
    monkeypatch.setattr(store._metadata, "replace_entities", paused)
    def writer():
        try:
            created.append(store.create("alice", "Sophie reads"))
        except BaseException as exc:
            errors.append(exc)
    def closer():
        close_requested.set()
        try:
            store.close()
        except BaseException as exc:
            errors.append(exc)
        finally:
            closed.set()
    worker = threading.Thread(target=writer)
    worker.start()
    assert reached.wait(5)
    closer_thread = threading.Thread(target=closer)
    closer_thread.start()
    try:
        assert close_requested.wait(5)
        assert not closed.is_set()
    finally:
        finish.set()
        worker.join(5)
        closer_thread.join(5)
    assert closed.is_set() and not errors
    assert not (root / records.PENDING).exists()
    with closing(sqlite3.connect(root / "entity_index.db")) as db:
        assert db.execute("SELECT COUNT(*) FROM entity_memory WHERE memory_id=?", (created[0]["id"],)).fetchone()[0] > 0
