import json
import os
import pytest

from test_personal_memory_backend import dependencies
from test_personal_legacy_recall import legacy_fixture
from plugins.builtin.sakura_mem0 import personal_records as records


def test_write_rehearsal_requires_admission_before_model_load(tmp_path, dependencies, monkeypatch):
    root, snapshot, _, calls = legacy_fixture(tmp_path, dependencies, monkeypatch, 384)
    with pytest.raises(ValueError, match='REHEARSAL'):
        records.open_personal_memory_from_snapshot(root, snapshot=snapshot, write_rehearsal=True)
    assert calls == []


def test_fresh_baseline_copy_writes_and_reopens_without_unlocking_default(tmp_path, dependencies, monkeypatch):
    from tools.personal_memory_write_rehearsal import prepare_rehearsal
    from plugins.builtin.sakura_mem0.index_contract import COPY_STATE_FILE
    baseline = tmp_path / 'baseline'
    source, snapshot, key, _ = legacy_fixture(baseline / 'data', dependencies, monkeypatch, 384)
    (baseline / COPY_STATE_FILE).write_text(json.dumps({'state': 'complete', 'role': 'baseline'}))
    with pytest.raises(Exception, match='PATH_INVALID'):
        prepare_rehearsal(baseline, baseline / 'must-not-create')
    assert not (baseline / 'must-not-create').exists()
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    target = tmp_path / 'rehearsal'
    prepare_rehearsal(baseline, target)
    store = records.open_personal_memory_from_snapshot(target, snapshot=snapshot, write_rehearsal=True)
    try:
        store.update('alice', key, 'updated synthetic remembered fact')
    finally:
        store.close()
    store = records.open_personal_memory_from_snapshot(target, snapshot=snapshot)
    try:
        assert store.get('alice', key)['memory'] == 'updated synthetic remembered fact'
        with pytest.raises(RuntimeError, match='RECALL_ONLY'):
            store.create('alice', 'must remain read only by default')
    finally:
        store.close()
    assert before == {p.relative_to(source): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    with pytest.raises(Exception, match='PATH_INVALID'):
        prepare_rehearsal(baseline, target)


def test_rehearsal_marker_cannot_be_reused_at_different_root(tmp_path, dependencies, monkeypatch):
    from plugins.builtin.sakura_mem0.index_contract import COPY_STATE_FILE
    root, snapshot, _, calls = legacy_fixture(tmp_path, dependencies, monkeypatch, 384)
    (root / COPY_STATE_FILE).write_text(json.dumps({'state': 'complete'}))
    (root / '.personal-write-rehearsal.json').write_text(json.dumps({
        'purpose': 'personal-memory-write-rehearsal', 'root': str(tmp_path / 'other')}))
    with pytest.raises(ValueError, match='REHEARSAL'):
        records.open_personal_memory_from_snapshot(root, snapshot=snapshot, write_rehearsal=True)
    assert calls == []


@pytest.mark.skipif(os.name != "nt", reason="Windows extended path admission")
@pytest.mark.parametrize("extended_marker", [False, True])
def test_write_admission_accepts_same_directory_with_extended_prefix(tmp_path, extended_marker):
    from pathlib import Path
    root = tmp_path / "memory"
    root.mkdir()
    extended = Path("\\\\?\\" + str(root))
    (root / ".sakura-personal-copy.json").write_text(json.dumps({"state": "complete"}), encoding="utf-8")
    marker = root / records.WRITE_REHEARSAL
    marker.write_text(json.dumps({"purpose": "personal-memory-write-rehearsal",
                                  "root": str(extended if extended_marker else root)}), encoding="utf-8")
    before = marker.read_bytes()
    records._require_write_rehearsal(root if extended_marker else extended)
    assert marker.read_bytes() == before
