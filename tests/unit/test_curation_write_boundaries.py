"""Curation must stop at failure/cancellation and safely replay committed facts."""
import json
import pytest

from plugins.builtin.sakura_mem0.domain_types import ChatHistoryEntry
from plugins.builtin.sakura_mem0.memory import _memory_metadata
from plugins.builtin.sakura_mem0.memory_curator import MemoryCurator, MemoryCurationError
from plugins.builtin.sakura_mem0.support import OperationCancelled


FACTS = ['用户喜欢樱花', '用户在学习日语', '用户周末练习游泳']


class Store:
    def __init__(self, *, fail_call=None, cancel_after=None):
        self.records, self.attempts = [], []
        self.fail_call, self.cancel_after = fail_call, cancel_after

    def list_memories(self, *, limit=None):
        return list(self.records) if limit is None else list(self.records[:limit])

    def create_memory(self, arguments, **kwargs):
        self.attempts.append(arguments['content'])
        if len(self.attempts) == self.fail_call:
            raise OSError('injected write failure')
        record = {'id': str(len(self.records)), 'content': arguments['content'],
                  'metadata': _memory_metadata(dict(arguments), scope_id='alice',
                      created_at='2026-09-21T00:00:00Z', updated_at='2026-09-21T00:00:00Z')}
        self.records.append(record)
        return {'memory': record}

    def check_cancelled(self):
        if self.cancel_after is not None and len(self.records) >= self.cancel_after:
            raise OperationCancelled()


def curator(store, facts=FACTS):
    class Api:
        def complete_raw(self, *args, **kwargs):
            return json.dumps({'operations': [{'op': 'add', 'content': text,
                'layer': 'semantic', 'confidence': .9} for text in facts]})
    return MemoryCurator(Api(), store)


def entries():
    return [ChatHistoryEntry(created_at='2026-09-21T00:00:00Z', role='user',
        content='我喜欢樱花，正在学日语，周末会游泳。', entry_id='evidence-1')]


def test_failed_write_stops_later_operations_and_identical_retry_deduplicates():
    store = Store(fail_call=2)
    worker = curator(store)
    with pytest.raises(MemoryCurationError, match='WRITE_FAILED'):
        worker.curate_entries(entries())
    assert store.attempts == FACTS[:2]
    assert [r['content'] for r in store.records] == FACTS[:1]
    store.fail_call = None
    result = worker.curate_entries(entries())
    assert result.created == 2 and result.ignored == 1
    assert [r['content'] for r in store.records] == FACTS
    assert all(r['metadata']['source_entry_ids'] == ['evidence-1'] for r in store.records)


@pytest.mark.parametrize('fact_count', [1, 3])
def test_cancel_after_successful_write_propagates_even_on_last_operation(fact_count):
    store = Store(cancel_after=1)
    worker = curator(store, FACTS[:fact_count])
    with pytest.raises(OperationCancelled):
        worker.curate_entries(entries(), cancel_checker=store.check_cancelled)
    assert store.attempts == FACTS[:1]
    store.cancel_after = None
    result = worker.curate_entries(entries())
    assert result.created == fact_count - 1
    assert [r['content'] for r in store.records] == FACTS[:fact_count]


def test_retry_checks_sources_beyond_first_500_records():
    store = Store()
    store.records = [{'id': f'old-{i}', 'content': f'独立工程记录{i}',
                      'metadata': {'source_entry_ids': [f'old-source-{i}']}}
                     for i in range(501)]
    store.create_memory({'content': FACTS[0], 'source_entry_ids': ['evidence-1']})
    store.attempts.clear()
    result = curator(store, FACTS[:1]).curate_entries(entries())
    assert result.created == 0 and result.ignored == 1
    assert store.attempts == []
    assert len(store.records) == 502
