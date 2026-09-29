"""Actual curator -> scoped personal Qdrant/SQLite, with no network calls."""
import json
import pytest

from test_personal_hybrid_writes import hybrid
from test_personal_memory_backend import dependencies, Encoder
from plugins.builtin.sakura_mem0 import personal_records as records
from plugins.builtin.sakura_mem0.domain_types import ChatHistoryEntry
from plugins.builtin.sakura_mem0.memory_curator import MemoryCurator


def facade(store, scope='alice'):
    from plugins.builtin.sakura_mem0.personal_curation import PersonalCurationStore
    return PersonalCurationStore(store, scope)


def test_personal_curation_prompt_only_advertises_supported_layers(hybrid):
    store, _, _ = hybrid
    prompt = MemoryCurator(None, facade(store))._build_self_curation_system_prompt()
    assert 'semantic=长期事实' in prompt
    assert 'episodic=已经发生的事件总结' in prompt
    assert 'core_profile=高度稳定的常驻档案' not in prompt


def run(store, operations, entry_id, *, cancel_checker=None):
    class Api:
        def complete_raw(self, *args, **kwargs):
            return json.dumps({'operations': operations})
    return MemoryCurator(Api(), store).curate_entries([
        ChatHistoryEntry(created_at='2026-09-21T00:00:00Z', role='user',
                         content='我正在学习日语。', entry_id=entry_id)], cancel_checker=cancel_checker)


def test_curator_update_preserves_sources_and_unknown_metadata_after_reopen(hybrid):
    store, root, identity = hybrid
    adapter = facade(store)
    assert run(adapter, [{'op': 'add', 'content': '用户正在学习日语',
                         'layer': 'semantic'}], 'entry-1').created == 1
    key = adapter.list_memories(limit=None)[0]['id']
    store.update('alice', key, '用户正在学习日语', metadata={'custom': 'preserved'})
    assert run(adapter, [{'op': 'update', 'id': key, 'content': '用户正在学习日语和法语',
                         'layer': 'semantic'}], 'entry-2').updated == 1
    store.close()
    reopened = records.open_personal_memory(root, identity=identity, encoder=Encoder(384))
    try:
        row = facade(reopened).list_memories(limit=None)[0]
        assert row['metadata']['source_entry_ids'] == ['entry-1', 'entry-2']
        assert row['metadata']['custom'] == 'preserved'
        assert row['content'] == '用户正在学习日语和法语'
        assert reopened.search('alice', '法语')['results'][0]['id'] == key
    finally:
        reopened.close()


def test_profile_candidates_are_opt_in_and_core_profile_crud_stays_closed(hybrid):
    store, _, _ = hybrid
    from plugins.builtin.sakura_mem0.personal_curation import PersonalCurationStore

    closed = facade(store)
    assert closed.allow_profile_candidates is False
    assert "core_candidate" not in MemoryCurator(None, closed)._build_self_curation_system_prompt()
    opened = PersonalCurationStore(store, "alice", profile_candidates=True)
    assert opened.allow_profile_candidates is True
    advertised = MemoryCurator(None, opened, accept_core_candidates=True)._build_self_curation_system_prompt()
    assert "core_candidate" in advertised
    with pytest.raises(ValueError, match="PERSONAL_CORE_PROFILE_UNSUPPORTED"):
        opened.create_memory({"content": "档案", "layer": "core_profile"})


def test_facade_scope_and_core_profile_guards(hybrid):
    store, root, _ = hybrid
    adapter = facade(store)
    key = store.create('bob', 'Bob private fact')['id']
    assert adapter.list_memories(limit=None) == []
    for call in (lambda: adapter.update_memory({'id': key, 'content': 'changed'}),
                 lambda: adapter.delete_memory({'id': key}),
                 lambda: adapter.create_memory({'content': 'fact', 'scope': 'bob'}),
                 lambda: adapter.create_memory({'content': 'fact', 'layer': 'core_profile'})):
        with pytest.raises(ValueError):
            call()
    assert store.get('bob', key)['memory'] == 'Bob private fact'
    assert not (root / records.PENDING).exists()


def test_facade_does_not_unlock_recall_only(hybrid):
    store, root, _ = hybrid
    store._recall_only = True
    with pytest.raises(RuntimeError, match='RECALL_ONLY'):
        facade(store).create_memory({'content': 'new fact'})
    assert not (root / records.PENDING).exists()


@pytest.mark.parametrize('interruption', ['cancel', 'sparse_failure'])
def test_actual_partial_batch_retry_keeps_committed_fact(hybrid, monkeypatch, interruption):
    from plugins.builtin.sakura_mem0.memory_curator import MemoryCurationError
    from plugins.builtin.sakura_mem0.support import OperationCancelled
    store, root, _ = hybrid
    adapter = facade(store)
    operations = [{'op': 'add', 'content': content, 'layer': 'semantic'} for content in
                  ['用户喜欢樱花', '用户周末练习游泳', '用户正在学习日语']]
    with store._session.operation() as backend:
        original = backend.vector_store._encode_bm25
        def encode(text):
            if '游泳' in text:
                return None
            return original(text)
        if interruption == 'sparse_failure':
            monkeypatch.setattr(backend.vector_store, '_encode_bm25', encode)
    def cancelled():
        if adapter.list_memories():
            raise OperationCancelled()
    with pytest.raises(OperationCancelled if interruption == 'cancel' else MemoryCurationError):
        run(adapter, operations, 'entry-1',
            cancel_checker=cancelled if interruption == 'cancel' else None)
    assert len(adapter.list_memories()) == 1
    assert not (root / records.PENDING).exists()
    monkeypatch.setattr(backend.vector_store, '_encode_bm25', original)
    result = run(adapter, operations, 'entry-1')
    assert result.created == 2 and result.ignored == 1
    assert len(adapter.list_memories()) == 3


def test_complete_scoped_snapshot_and_curator_delete(hybrid):
    store, _, _ = hybrid
    adapter = facade(store)
    for index in range(130):
        store.create('alice', f'Independent fact {index}')
    store.create('bob', 'Unrelated private fact')
    rows = adapter.list_memories(limit=None)
    assert len(rows) == 130
    assert len(adapter.list_memories(limit=3)) == 3
    key = rows[-1]['id']
    run(adapter, [{'op': 'delete', 'id': key}], 'entry-2')
    assert store.get('alice', key) is None
    assert len(adapter.list_memories()) == 129
    assert len(store.list('bob')) == 1


def test_curator_persistent_failure_keeps_pending_and_blocks_retry(hybrid, monkeypatch):
    from plugins.builtin.sakura_mem0.memory_curator import MemoryCurationError
    store, root, _ = hybrid
    adapter = facade(store)
    def fail(*args, **kwargs):
        raise OSError('injected entity write failure')
    monkeypatch.setattr(store._metadata, 'replace_entities', fail)
    with pytest.raises(MemoryCurationError, match='WRITE_FAILED'):
        run(adapter, [{'op': 'add', 'content': '用户喜欢樱花', 'layer': 'semantic'},
                      {'op': 'add', 'content': '用户学习日语', 'layer': 'semantic'}], 'entry-1')
    assert (root / records.PENDING).exists()
    with pytest.raises(RuntimeError, match='INCOMPLETE'):
        adapter.list_memories()
    with pytest.raises(RuntimeError, match='INCOMPLETE'):
        adapter.create_memory({'content': 'must not write'})
    with store._session.operation() as backend:
        assert backend.vector_store.client.count('sakura_memories').count == 1
