"""Hybrid write safety on disposable synthetic Qdrant/SQLite only."""
import pytest
import shutil

from test_personal_memory_backend import dependencies, Encoder
from test_personal_memory_records import fixture_memory
from plugins.builtin.sakura_mem0 import personal_records as records


@pytest.fixture
def hybrid(tmp_path, dependencies, monkeypatch):
    from qdrant_client import QdrantClient, models
    from mem0.vector_stores.qdrant import Qdrant
    root, identity = fixture_memory(tmp_path, dependencies)
    client = QdrantClient(path=str(root / 'indexes' / ('a' * 32) / 'qdrant'))
    try:
        client.delete_collection('sakura_memories')  # Empty synthetic collection only.
        client.create_collection('sakura_memories',
            vectors_config=models.VectorParams(size=384, distance=models.Distance.COSINE),
            sparse_vectors_config={'bm25': models.SparseVectorParams()})
    finally:
        client.close()
    monkeypatch.setattr(Qdrant, '_encode_bm25', lambda self, text:
        models.SparseVector(indices=[9 if 'updated' in text.lower() else 7], values=[1.0]))
    store = records.open_personal_memory(root, identity=identity, encoder=Encoder(384))
    try:
        yield store, root, identity
    finally:
        store.close()


@pytest.mark.parametrize('operation', ['create', 'update'])
def test_missing_sparse_encoder_rejects_before_any_mutation(hybrid, monkeypatch, operation):
    store, root, _ = hybrid
    key = store.create('alice', 'Original Alice fact')['id']
    with store._session.operation() as backend:
        client = backend.vector_store.client
        before = client.retrieve('sakura_memories', [key], with_vectors=True)
        history = backend.history(key)
        entities = store._metadata.entities.execute('SELECT * FROM entity_memory').fetchall()
        monkeypatch.setattr(backend.vector_store, '_encode_bm25', lambda text: None)
    with pytest.raises(RuntimeError, match='PERSONAL_BM25_WRITE_UNAVAILABLE'):
        if operation == 'create':
            store.create('alice', 'Updated Alice fact')
        else:
            store.update('alice', key, 'Updated Alice fact')
    assert not (root / records.PENDING).exists()
    with store._session.operation() as backend:
        assert client.count('sakura_memories').count == 1
        assert client.retrieve('sakura_memories', [key], with_vectors=True) == before
        assert backend.history(key) == history
        assert store._metadata.entities.execute('SELECT * FROM entity_memory').fetchall() == entities


def test_hybrid_update_refreshes_sparse_and_delete_preserves_unrelated_orphans(hybrid):
    store, _, _ = hybrid
    key = store.create('alice', 'Original Alice fact', metadata={'source_entry_ids': ['source-1']})['id']
    with store._metadata.entities:
        store._metadata.entities.execute('INSERT INTO entity_memory VALUES(?,?,?)', ('orphan', 'absent', 'old'))
    with store._metadata.access:
        store._metadata.access.execute('INSERT INTO memory_access VALUES(?,?)', ('absent', 'old'))
    store.update('alice', key, 'Updated Alice fact')
    with store._session.operation() as backend:
        row = backend.vector_store.client.retrieve('sakura_memories', [key], with_vectors=True)[0]
        assert row.vector['bm25'].indices == [9]
        assert row.payload['source_entry_ids'] == ['source-1']
    store.delete('alice', key)
    assert store.get('alice', key) is None
    assert store._metadata.entities.execute('SELECT * FROM entity_memory').fetchall() == [('orphan', 'absent', 'old')]
    assert store._metadata.access.execute('SELECT * FROM memory_access').fetchall() == [('absent', 'old')]


@pytest.mark.parametrize('failure_stage', ['history', 'entities'])
def test_partial_write_blocks_reopen_and_closed_snapshot_recovers_to_new_root(hybrid, tmp_path, monkeypatch, failure_stage):
    store, root, identity = hybrid
    key = store.create('alice', 'Original Alice fact')['id']
    store.close()
    before = tmp_path / 'before-write'
    shutil.copytree(root, before)  # All database clients are closed.
    failed = records.open_personal_memory(root, identity=identity, encoder=Encoder(384))
    def fail(*args, **kwargs):
        raise OSError('injected persistent-store failure')
    try:
        with failed._session.operation() as backend:
            original = backend.vector_store._encode_bm25
            if failure_stage == 'history':
                monkeypatch.setattr(backend.db, 'add_history', fail)
            else:
                monkeypatch.setattr(failed._metadata, 'replace_entities', fail)
        with pytest.raises(OSError, match='injected'):
            failed.update('alice', key, 'Updated Alice fact')
        assert backend.vector_store._encode_bm25 == original
        with pytest.raises(RuntimeError, match='INCOMPLETE'):
            failed.search('alice', 'Alice')
    finally:
        failed.close()
    with pytest.raises(RuntimeError, match='INCOMPLETE'):
        records.open_personal_memory(root, identity=identity, encoder=Encoder(384))
    recovered_root = tmp_path / 'recovered'
    shutil.copytree(before, recovered_root)
    recovered = records.open_personal_memory(recovered_root, identity=identity, encoder=Encoder(384))
    try:
        assert recovered.get('alice', key)['memory'] == 'Original Alice fact'
        with recovered._session.operation() as backend:
            row = backend.vector_store.client.retrieve('sakura_memories', [key], with_vectors=True)[0]
            assert row.vector['bm25'].indices == [7]
            assert [item['event'] for item in backend.history(key)] == ['ADD']
        assert recovered.lookup_entities('alice', ['Original Alice']) == [key]
    finally:
        recovered.close()
    assert (root / records.PENDING).exists()  # Failed copy remains available for diagnosis.
