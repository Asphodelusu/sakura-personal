import sys
from types import SimpleNamespace

import pytest

from plugins.builtin.sakura_mem0 import personal_records
from test_personal_memory_backend import dependencies
from test_personal_model_loading import model_fixture


def legacy_fixture(tmp_path, dependencies, monkeypatch, dimensions):
    from qdrant_client import QdrantClient, models
    from uuid import uuid4
    root, snapshot, identity, calls = model_fixture(tmp_path, dependencies, monkeypatch, dimensions, legacy=True)
    # Build the intended old layout directly. Renaming an opened SQLite-backed
    # directory is not part of the legacy admission contract being tested.
    key = str(uuid4())
    client = QdrantClient(path=str(root / 'qdrant'))
    try:
        client.upsert('sakura_memories', [models.PointStruct(id=key,
            vector=[1.] + [0.] * (dimensions - 1),
            payload={'data': 'synthetic remembered fact', 'user_id': 'alice'})])
    finally:
        client.close()
    history = dependencies[1].SQLiteManager(str(root / 'mem0_history.db'))
    try:
        history.add_history(key, None, 'synthetic remembered fact', 'ADD')
    finally:
        history.close()
    return root, snapshot, key, calls


@pytest.mark.parametrize("dimensions", [384, 1024])
def test_legacy_recalls_without_relabeling_and_rejects_writes(tmp_path, dependencies, monkeypatch, dimensions):
    root, snapshot, key, calls = legacy_fixture(tmp_path, dependencies, monkeypatch, dimensions)
    store = personal_records.open_personal_memory_from_snapshot(root, snapshot=snapshot)
    try:
        assert store.search("alice", "remembered")["results"][0]["id"] == key
        assert store.search("bob", "remembered")["results"] == []
        for operation in (lambda: store.create("alice", "new"),
                          lambda: store.update("alice", key, "updated"),
                          lambda: store.delete("alice", key),
                          lambda: store.record_access("alice", [key], when="2026-09-20T00:00:00Z")):
            with pytest.raises(RuntimeError, match="PERSONAL_LEGACY_RECALL_ONLY"):
                operation()
        assert store.get("alice", key)["memory"] == "synthetic remembered fact"
    finally:
        store.close()
    assert len(calls) == 1
    assert not (root / "active_index.json").exists()
    assert not (root / "indexes").exists()
    assert not (root / "personal_write_pending.json").exists()


@pytest.mark.parametrize("damage", ["missing_marker", "wrong_marker", "broken_pointer", "missing_storage"])
def test_legacy_rejection_happens_before_model_loading(tmp_path, dependencies, monkeypatch, damage):
    root, snapshot, _, calls = legacy_fixture(tmp_path, dependencies, monkeypatch, 384)
    if damage == "missing_marker":
        (root / "embedding_version.txt").unlink()
    elif damage == "wrong_marker":
        (root / "embedding_version.txt").write_text("unknown-model:384")
    elif damage == "broken_pointer":
        (root / "active_index.json").write_text("broken")
    else:
        (root / "qdrant/collection/sakura_memories/storage.sqlite").unlink()
    with pytest.raises(ValueError, match="PERSONAL_INDEX"):
        personal_records.open_personal_memory_from_snapshot(root, snapshot=snapshot)
    assert not calls


def test_legacy_preserves_and_queries_bm25_with_scope_filter(tmp_path, dependencies, monkeypatch):
    from qdrant_client import QdrantClient, models
    from mem0.vector_stores.qdrant import Qdrant
    root, snapshot, key, _ = legacy_fixture(tmp_path, dependencies, monkeypatch, 384)
    client = QdrantClient(path=str(root / "qdrant"))
    sparse = models.SparseVector(indices=[7], values=[1.0])
    try:
        row = client.retrieve("sakura_memories", [key], with_vectors=True)[0]
        client.delete_collection("sakura_memories")
        client.create_collection("sakura_memories", vectors_config=models.VectorParams(size=384, distance=models.Distance.COSINE),
                                 sparse_vectors_config={"bm25": models.SparseVectorParams()})
        client.upsert("sakura_memories", [models.PointStruct(id=key, vector=row.vector, payload=row.payload)])
        client.update_vectors("sakura_memories", [models.PointVectors(id=key, vector={"bm25": sparse})])
    finally:
        client.close()
    queries = []
    original = Qdrant.keyword_search
    def keyword(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        queries.append((kwargs["filters"], len(result or [])))
        return result
    monkeypatch.setattr(Qdrant, "keyword_search", keyword)
    monkeypatch.setattr(Qdrant, "_encode_bm25", lambda self, text: sparse)
    store = personal_records.open_personal_memory_from_snapshot(root, snapshot=snapshot)
    try:
        assert store.search("alice", "remembered")["results"]
        assert store.search("bob", "remembered")["results"] == []
        with store._session.operation() as backend:
            row = backend.vector_store.client.retrieve("sakura_memories", [key], with_vectors=True)[0]
            assert row.vector["bm25"] == sparse
    finally:
        store.close()
    assert queries == [({"user_id": "alice"}, 1), ({"user_id": "bob"}, 0)]


def test_same_dimension_wrong_legacy_encoder_is_rejected_and_store_is_released(tmp_path, dependencies, monkeypatch):
    from qdrant_client import QdrantClient
    root, snapshot, _, _ = legacy_fixture(tmp_path, dependencies, monkeypatch, 384)
    model = sys.modules["sentence_transformers"].SentenceTransformer
    monkeypatch.setattr(model, "encode", lambda *args, **kwargs: SimpleNamespace(tolist=lambda: [0., 1.] + [0.] * 382))
    with pytest.raises(ValueError, match="PERSONAL_LEGACY_VECTOR_MISMATCH"):
        personal_records.open_personal_memory_from_snapshot(root, snapshot=snapshot)
    client = QdrantClient(path=str(root / "qdrant"))
    try:
        assert client.count("sakura_memories").count == 1
    finally:
        client.close()


def test_personal_search_respects_requested_limit(tmp_path, dependencies, monkeypatch):
    root, snapshot, _, _ = model_fixture(tmp_path, dependencies, monkeypatch, 384)
    store = personal_records.open_personal_memory_from_snapshot(root, snapshot=snapshot)
    try:
        for text in ("first fact", "second fact", "third fact"):
            store.create("alice", text)
        assert len(store.search("alice", "fact", limit=1)["results"]) == 1
    finally:
        store.close()


def test_legacy_plugin_boundary_becomes_ready_and_exposes_only_recall(tmp_path, dependencies, monkeypatch):
    from app.legacy_import.personal_copy import prepare_personal_copy
    from plugins.builtin.sakura_mem0.plugin import SakuraMem0Plugin
    from test_personal_plugin_runtime import Context
    source, work = tmp_path / "source", tmp_path / "work"
    legacy_fixture(source / "data", dependencies, monkeypatch, 384)
    prepare_personal_copy(source, work, source_is_quiescent=lambda: True)
    context = Context(work)
    SakuraMem0Plugin(personal_snapshot=work / "data/snapshot").setup(context)
    try:
        assert [descriptor["name"] for descriptor, _ in context.tools] == ["memory_search"]
        search = context.tools[0][1]
        search.__self__._boundary._thread.join(5)
        result = search({"query": "remembered", "limit": 1})
        assert result["status"] == "ready"
        assert len(result["memories"]) == 1
        assert context.providers[0][1]({"character_id": "bob", "current_input": "remembered"}) == []
    finally:
        context.effects[-1]()
    assert search({"query": "remembered"})["status"] == "stopped"
