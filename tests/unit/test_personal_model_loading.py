import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from plugins.builtin.sakura_mem0 import personal_records
from test_personal_memory_backend import dependencies
from test_personal_memory_records import fixture_memory


def model_fixture(tmp_path, dependencies, monkeypatch, dimensions, *, legacy=False):
    root, identity = fixture_memory(tmp_path, dependencies, dimensions, legacy=legacy)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "weights").write_bytes(b"synthetic weights")
    identity["artifact_sha256"] = hashlib.sha256(json.dumps([
        "weights", hashlib.sha256(b"synthetic weights").hexdigest()
    ], ensure_ascii=False).encode()).hexdigest()
    for path in (() if legacy else (root / "active_index.json", root / "indexes" / ("a" * 32) / "journal.json")):
        value = json.loads(path.read_text())
        value["encoding_identity"] = identity
        path.write_text(json.dumps(value))
    calls = []
    class Model:
        def __init__(self, path, **kwargs):
            calls.append((path, kwargs))
        def get_sentence_embedding_dimension(self):
            return dimensions
        def encode(self, text, **kwargs):
            assert self.max_seq_length == identity["max_seq_length"]
            assert kwargs == {"convert_to_numpy": True}
            return SimpleNamespace(tolist=lambda: [1.] + [0.] * (dimensions - 1))
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=Model))
    return root, snapshot, identity, calls


@pytest.mark.parametrize("dimensions", [384, 1024])
def test_snapshot_entry_opens_recalls_scoped_records_and_reopens(tmp_path, dependencies, monkeypatch, dimensions):
    root, snapshot, _, calls = model_fixture(tmp_path, dependencies, monkeypatch, dimensions)
    for _ in range(2):
        store = personal_records.open_personal_memory_from_snapshot(root, snapshot=snapshot)
        try:
            store.create("alice", "synthetic fact")
            assert store.search("alice", "fact")["results"]
            assert store.search("bob", "fact")["results"] == []
        finally:
            store.close()
        with pytest.raises(RuntimeError, match="CLOSED"):
            store.search("alice", "fact")
    assert len(calls) == 2
    assert calls[0] == (str(snapshot.resolve()), {"local_files_only": True, "trust_remote_code": False})


@pytest.mark.parametrize("damage", ["weights", "missing", "binding", "metadata"])
def test_rejects_bad_model_or_storage_before_loading_model(tmp_path, dependencies, monkeypatch, damage):
    root, snapshot, _, calls = model_fixture(tmp_path, dependencies, monkeypatch, 384)
    if damage == "weights":
        (snapshot / "weights").write_bytes(b"different weights")
    elif damage == "missing":
        snapshot = tmp_path / "missing"
    elif damage == "metadata":
        (root / "entity_index.db").unlink()
    else:
        (root / "active_index.json").write_text("{}")
    with pytest.raises(ValueError):
        personal_records.open_personal_memory_from_snapshot(root, snapshot=snapshot)
    assert calls == []


def test_migration_inspection_uses_verified_snapshot_path(tmp_path, dependencies, monkeypatch):
    from app.legacy_import.memory_contract import inspect_personal_migration
    from app.legacy_import.personal_copy import prepare_personal_copy
    from app.storage.timeline import TimelineStore
    source = tmp_path / "source"
    _, snapshot, _, calls = model_fixture(source / "data", dependencies, monkeypatch, 384)
    TimelineStore(source / "data/chat_history/timeline.sqlite3").initialize()
    work = tmp_path / "work"
    prepare_personal_copy(source, work, source_is_quiescent=lambda: True)
    result = inspect_personal_migration(work, snapshot=work / "data/snapshot")
    assert result["ok"]
    assert len(calls) == 1


@pytest.mark.parametrize("damage", ["dimension", "changes_during_load"])
def test_loaded_model_is_checked_before_backend_open(tmp_path, dependencies, monkeypatch, damage):
    root, snapshot, _, calls = model_fixture(tmp_path, dependencies, monkeypatch, 384)
    original = sys.modules["sentence_transformers"].SentenceTransformer
    class BadModel(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if damage == "changes_during_load":
                (snapshot / "weights").write_bytes(b"changed")
        def get_sentence_embedding_dimension(self):
            return 1024 if damage == "dimension" else 384
    monkeypatch.setattr(sys.modules["sentence_transformers"], "SentenceTransformer", BadModel)
    with pytest.raises(ValueError, match="PERSONAL_MODEL"):
        personal_records.open_personal_memory_from_snapshot(root, snapshot=snapshot)
    assert len(calls) == 1
