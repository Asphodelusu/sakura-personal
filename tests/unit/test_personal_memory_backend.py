"""Real bundled mem0/Qdrant/SQLite; deterministic encoders, no model service."""
import json
import socket
import threading
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.builtin.sakura_mem0 import personal_backend as personal


class Encoder:
    def __init__(self, dimensions):
        self.dimensions = dimensions
        self.closed = False

    def embed(self, text, *args, **kwargs):
        return [1.0] + [0.0] * (self.dimensions - 1)

    def close(self):
        self.closed = True


@pytest.fixture
def dependencies(tmp_path, monkeypatch):
    plugin = Path(__file__).resolve().parents[2] / "plugins/builtin/sakura_mem0"
    monkeypatch.syspath_prepend(str(plugin))
    monkeypatch.setenv("MEM0_DIR", str(tmp_path / "mem0-home"))
    monkeypatch.setenv("MEM0_TELEMETRY", "False")
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("network forbidden"))
    from plugins.builtin.sakura_mem0.memory import _import_mem0_dependencies
    parts = _import_mem0_dependencies()
    vendor = parts[1]
    monkeypatch.setattr(vendor, "MEM0_TELEMETRY", False)
    monkeypatch.setattr(vendor, "capture_event", lambda *a, **k: None)
    monkeypatch.setattr(vendor, "extract_entities", lambda text: [])
    monkeypatch.setattr(vendor, "extract_entities_batch", lambda texts: [[] for _ in texts])
    monkeypatch.setattr(vendor, "lemmatize_for_bm25", lambda text: text)
    monkeypatch.setattr(vendor.LlmFactory, "create", lambda *a, **k: pytest.fail("LLM forbidden"))
    from mem0.vector_stores.qdrant import Qdrant
    monkeypatch.setattr(Qdrant, "_get_bm25_encoder", lambda self: None)
    return parts


def fixture_index(tmp_path, dimensions, dependencies, *, legacy=False):
    from qdrant_client import QdrantClient, models
    root = tmp_path / "memory"
    stage = root if legacy else root / "indexes" / ("a" * 32)
    stage.mkdir(parents=True)
    identity = {
        "schema": 1,
        "model_id": "BAAI/bge-m3" if dimensions == 1024 else "sentence-transformers/all-MiniLM-L6-v2",
        "artifact_sha256": "b" * 64,
        "dimensions": dimensions,
        "max_seq_length": 512 if dimensions == 1024 else 256,
        "encoder": "mem0-huggingface-sentence-transformers-encode-v1",
        "document_encoding": "encode-default", "query_encoding": "encode-default",
        "normalize_embeddings": False,
    }
    with closing(QdrantClient(path=str(stage / "qdrant"))) as client:
        client.create_collection("sakura_memories", vectors_config=models.VectorParams(size=dimensions, distance=models.Distance.COSINE))
        client.create_collection("sakura_memories_entities", vectors_config=models.VectorParams(size=dimensions, distance=models.Distance.COSINE))
    history = dependencies[1].SQLiteManager(str(root / "mem0_history.db"))
    history.close()
    if legacy:
        (root / "embedding_version.txt").write_text(f'{identity["model_id"]}:{dimensions}')
        return root, identity
    binding = {"index_id": "a" * 32, "encoding_identity": identity}
    (root / "active_index.json").write_text(json.dumps(binding), encoding="utf-8")
    (stage / "journal.json").write_text(json.dumps({"state": "validated", "encoding_identity": identity,
        "counts": {"sakura_memories": 0, "sakura_memories_entities": 0}}), encoding="utf-8")
    (root / "qdrant").mkdir()  # Must not select the stale legacy root.
    return root, identity


@pytest.mark.parametrize("dimensions", [384, 1024])
def test_personal_raw_crud_history_entities_and_restart(tmp_path, dependencies, dimensions):
    root, identity = fixture_index(tmp_path, dimensions, dependencies)
    session = personal.open_personal_backend(root, identity=identity, encoder=Encoder(dimensions))
    with session.operation() as backend:
        record = backend.add("synthetic fact", user_id="alice", metadata={"source_entry_ids": ["entry-1"]}, infer=False)["results"][0]
        key = record["id"]
        backend._upsert_entity("fixture", "ORG", key, {"user_id": "alice"})
        assert backend.vector_store.client.count("sakura_memories_entities").count == 1
        assert backend.search("synthetic", filters={"user_id": "bob"})["results"] == []
        # The mem0 raw API replaces metadata; the application owns the merge.
        backend.update(key, "updated fact", metadata=backend.get(key)["metadata"])
        history = backend.history(key)
        assert len(history) >= 2
    session.close()
    reopened = personal.open_personal_backend(root, identity=identity, encoder=Encoder(dimensions))
    try:
        with reopened.operation() as backend:
            assert backend.get(key)["memory"] == "updated fact"
            assert backend.get(key)["metadata"]["source_entry_ids"] == ["entry-1"]
            assert backend.history(key) == history
            backend.delete(key)
            assert backend.get(key) is None
            assert backend.vector_store.client.count("sakura_memories_entities").count == 0
    finally:
        reopened.close()
    assert not any((root / "qdrant").iterdir())


@pytest.mark.parametrize("damage", ["identity", "missing_storage", "missing_history", "broken_pointer", "missing_entities", "empty_history", "corrupt_storage"])
def test_rejects_incomplete_index_before_encoder_or_database_open(tmp_path, dependencies, damage):
    root, identity = fixture_index(tmp_path, 384, dependencies)
    stage = root / "indexes" / ("a" * 32)
    if damage == "identity":
        identity = {**identity, "normalize_embeddings": True}
    elif damage == "missing_storage":
        (stage / "qdrant/collection/sakura_memories/storage.sqlite").unlink()
    elif damage == "missing_entities":
        (stage / "qdrant/collection/sakura_memories_entities/storage.sqlite").unlink()
    elif damage == "missing_history":
        (root / "mem0_history.db").unlink()
    elif damage == "empty_history":
        (root / "mem0_history.db").write_bytes(b"")
    elif damage == "corrupt_storage":
        (stage / "qdrant/collection/sakura_memories/storage.sqlite").write_bytes(b"broken")
    else:
        (root / "active_index.json").write_text("{", encoding="utf-8")
    encoder = SimpleNamespace(embed=lambda *a, **k: pytest.fail("encoder reached"))
    with pytest.raises(ValueError, match="PERSONAL_INDEX"):
        personal.open_personal_backend(root, identity=identity, encoder=encoder)


def test_close_waits_until_associated_metadata_write_finishes(tmp_path, dependencies):
    root, identity = fixture_index(tmp_path, 384, dependencies)
    encoder = Encoder(384)
    session = personal.open_personal_backend(root, identity=identity, encoder=encoder)
    wrote = threading.Event()
    finish = threading.Event()
    closed = threading.Event()
    errors = []
    def writer():
        try:
            with session.operation() as backend:
                backend.add("fact", user_id="alice", infer=False)
                wrote.set()
                assert finish.wait(5)
                assert not encoder.closed
                (root / "metadata.json").write_text("completed", encoding="utf-8")
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=writer)
    thread.start()
    assert wrote.wait(5)
    closer = threading.Thread(target=lambda: (session.close(), closed.set()))
    closer.start()
    try:
        assert session.closing.wait(5)
        assert not closed.is_set()
        with pytest.raises(RuntimeError, match="CLOSED"):
            with session.operation():
                pytest.fail("accepted after close requested")
    finally:
        finish.set()
        thread.join(5)
        closer.join(5)
    assert not errors and closed.is_set()
    assert (root / "metadata.json").read_text() == "completed"


@pytest.mark.parametrize("value", [[], [float("nan")] * 384, [True] * 384])
def test_invalid_encoder_output_is_rejected_before_any_backend_write(tmp_path, dependencies, value):
    root, identity = fixture_index(tmp_path, 384, dependencies)
    encoder = Encoder(384)
    encoder.embed = lambda *a, **k: value
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="VECTOR_INVALID"):
        personal.open_personal_backend(root, identity=identity, encoder=encoder)
    assert encoder.closed
    assert before == {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_cannot_close_inside_operation_and_nested_lease_remains_usable(tmp_path, dependencies):
    root, identity = fixture_index(tmp_path, 384, dependencies)
    session = personal.open_personal_backend(root, identity=identity, encoder=Encoder(384))
    try:
        with session.operation() as backend:
            with pytest.raises(RuntimeError, match="ACTIVE_OPERATION"):
                session.close()
            with session.operation() as nested:
                assert nested is backend
                assert nested.get_all(filters={"user_id": "alice"})["results"] == []
    finally:
        session.close()


def test_runtime_bad_vector_preserves_existing_memory_and_history(tmp_path, dependencies):
    root, identity = fixture_index(tmp_path, 384, dependencies)
    encoder = Encoder(384)
    session = personal.open_personal_backend(root, identity=identity, encoder=encoder)
    try:
        with session.operation() as backend:
            key = backend.add("original", user_id="alice", infer=False)["results"][0]["id"]
            original = backend.get(key)
            history = backend.history(key)
            encoder.dimensions = 1024
            with pytest.raises(ValueError, match="VECTOR_INVALID"):
                backend.update(key, "invalid update", metadata=original["metadata"])
            assert backend.get(key) == original
            assert backend.history(key) == history
    finally:
        session.close()
