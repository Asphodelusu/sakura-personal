from pathlib import Path

import pytest

from plugins.builtin.sakura_mem0.memory import MemoryStore, validate_existing_memory_store
from app.legacy_import.importer import run_legacy_import, _validate_memory
from app.legacy_import.incremental import inspect_character_data_import, _qdrant_points
from app.legacy_import.errors import LegacyImportError
from app.legacy_import.models import LegacyInspection
from app.legacy_import.inspector import inspect_installation

CODE = "MEMORY_PERSONAL_INDEX_UNSUPPORTED"
IMPORT_CODE = "LEGACY_PERSONAL_MEMORY_UNSUPPORTED"


def mark(root, marker):
    memory = root / "data" / "memory"
    memory.mkdir(parents=True)
    if marker == "indexes":
        (memory / marker).mkdir()
    else:
        (memory / marker).write_text("malformed private sentinel", encoding="utf-8")
    # A stale legacy root must never mask the authoritative personal marker.
    (memory / "qdrant").mkdir()
    (memory / "qdrant/keep.txt").write_text("existing store sentinel", encoding="utf-8")
    return memory


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
            for path in root.rglob("*")}


@pytest.mark.parametrize("marker", ["active_index.json", "indexes"])
def test_store_rejects_personal_layout_before_starting_any_backend(tmp_path, marker):
    memory = mark(tmp_path, marker)
    before = snapshot(tmp_path)
    with pytest.raises(RuntimeError, match=CODE):
        MemoryStore(base_dir=tmp_path, memory_dir=memory, memory_client=object())
    assert snapshot(tmp_path) == before


def test_configuration_rechecks_layout_before_backend_creation(tmp_path):
    memory = tmp_path / "data/memory"
    store = MemoryStore(base_dir=tmp_path, memory_client=object(), memory_dir=memory)
    try:
        memory.mkdir(parents=True, exist_ok=True)
        (memory / "active_index.json").write_text("{}", encoding="utf-8")
        with pytest.raises(RuntimeError, match=CODE):
            store.build_local_backend_config()
        assert not (memory / "qdrant").exists()
    finally:
        store.close()


@pytest.mark.parametrize("method", [validate_existing_memory_store, _validate_memory, _qdrant_points])
def test_validators_never_ignore_or_quarantine_personal_layout(tmp_path, method):
    memory = mark(tmp_path, "indexes")
    before = snapshot(tmp_path)
    with pytest.raises(RuntimeError, match="PERSONAL_MEMORY_UNSUPPORTED|PERSONAL_INDEX_UNSUPPORTED"):
        method(memory)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("side", ["source", "target"])
def test_first_import_refuses_personal_data_before_staging_even_with_cached_inspection(tmp_path, side, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    mark(tmp_path / side, "active_index.json")
    before = snapshot(tmp_path)
    inspection = LegacyInspection(1, True, "0.9.10", "windows", "synthetic", False, 0, 10000000, {})
    monkeypatch.setattr("app.legacy_import.importer._copy_memory",
                        lambda *args, **kwargs: pytest.fail("copy reached before personal-layout guard"))
    with pytest.raises(LegacyImportError, match=IMPORT_CODE):
        run_legacy_import(source, target, inspection=inspection)
    assert snapshot(tmp_path) == before


def test_incremental_import_reports_personal_data_before_converting_history(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    mark(source, "active_index.json")
    before = snapshot(tmp_path)
    with pytest.raises(LegacyImportError, match=IMPORT_CODE):
        inspect_character_data_import(source, target)
    assert snapshot(tmp_path) == before


def test_empty_upstream_layout_remains_usable(tmp_path):
    memory = tmp_path / "data/memory"
    store = MemoryStore(base_dir=tmp_path, memory_dir=memory, memory_client=object())
    try:
        config = store.build_local_backend_config()
        assert config["vector_store"]["config"]["embedding_model_dims"] == 384
        assert Path(config["vector_store"]["config"]["path"]) == memory / "qdrant"
        validate_existing_memory_store(memory)
    finally:
        store.close()


@pytest.mark.parametrize("side", ["source", "target"])
def test_preview_reports_content_free_personal_layout_blocker(tmp_path, side):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    mark(tmp_path / side, "active_index.json")
    before = snapshot(tmp_path)
    inspection = inspect_installation(source, target)
    assert not inspection.compatible
    assert {"code": IMPORT_CODE, "stage": "inspect"} in inspection.blockers
    assert "private sentinel" not in str(inspection.blockers)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("marker", ["active_index.json", "indexes"])
def test_wrong_marker_type_does_not_look_like_an_empty_store(tmp_path, marker):
    memory = tmp_path / "data/memory"
    memory.mkdir(parents=True)
    if marker == "active_index.json":
        (memory / marker).mkdir()
    else:
        (memory / marker).write_text("invalid", encoding="utf-8")
    with pytest.raises(RuntimeError, match=CODE):
        validate_existing_memory_store(memory)


def test_broken_personal_marker_is_not_ignored(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    try:
        (memory / "active_index.json").symlink_to(memory / "absent.json")
    except OSError:
        pytest.skip("当前账户无法创建符号链接")
    with pytest.raises(RuntimeError, match=CODE):
        validate_existing_memory_store(memory)
