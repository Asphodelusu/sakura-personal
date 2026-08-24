from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.agent.memory import CoreProfileStorageError, MemoryStore
from app.storage.paths import StoragePaths


def _store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(base_dir=tmp_path, scope_id="Sakura", memory_client=object())


def _core_path(tmp_path: Path) -> Path:
    return StoragePaths(tmp_path).memory_core_profiles()


def _write_profile(tmp_path: Path, record: dict[str, Any]) -> Path:
    path = _core_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"id": "core_profile:Sakura", **record}
    path.write_text(
        json.dumps({"Sakura": payload}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _saved_record(tmp_path: Path) -> dict[str, Any]:
    return json.loads(_core_path(tmp_path).read_text(encoding="utf-8"))["Sakura"]


def test_v1_read_returns_content_without_touching_file(tmp_path: Path) -> None:
    path = _core_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps(
        {"Sakura": {"id": "core_profile:Sakura", "content": "原文", "memory": "原文", "metadata": {}}},
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    path.write_text(original, encoding="utf-8")

    record = _store(tmp_path).core_profile()

    assert record is not None
    assert record["content"] == "原文"
    assert path.read_text(encoding="utf-8") == original


def test_v2_reads_content_without_rewriting(tmp_path: Path) -> None:
    _write_profile(tmp_path, {"schema_version": 2, "content": "缓存正文", "memory": "缓存正文", "sections": {"legacy": "原文"}, "metadata": {}})
    assert _store(tmp_path).core_profile()["content"] == "缓存正文"


def test_v2_missing_content_renders_sections_read_only(tmp_path: Path) -> None:
    path = _write_profile(tmp_path, {"schema_version": 2, "sections": {"legacy": "第一段", "今の関係": "第二段"}, "metadata": {}})
    before = path.read_bytes()
    record = _store(tmp_path).core_profile()
    assert record is not None
    assert record["content"] == "第一段\n\n第二段"
    assert path.read_bytes() == before


def test_unknown_schema_with_content_is_read_only_compatible(tmp_path: Path) -> None:
    _write_profile(tmp_path, {"schema_version": 99, "content": "未来正文", "metadata": {}})
    assert _store(tmp_path).core_profile()["content"] == "未来正文"


def test_unknown_schema_without_content_does_not_interpret_future_sections(
    tmp_path: Path,
) -> None:
    _write_profile(
        tmp_path,
        {"schema_version": 99, "sections": {"future": "未来正文"}, "metadata": {}},
    )

    assert _store(tmp_path).core_profile() is None


@pytest.mark.parametrize(
    "sections",
    [
        {"legacy": "正文", "invalid": 123},
        {"legacy": "正文", "empty": "   "},
    ],
)
def test_v2_invalid_sections_do_not_partially_render(
    tmp_path: Path,
    sections: dict[Any, Any],
) -> None:
    _write_profile(
        tmp_path,
        {"schema_version": 2, "sections": sections, "metadata": {}},
    )

    assert _store(tmp_path).core_profile() is None


def test_corrupt_json_read_returns_none_without_overwrite(tmp_path: Path) -> None:
    path = _core_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    before = path.read_bytes()
    assert _store(tmp_path).core_profile() is None
    assert path.read_bytes() == before


def test_v1_next_write_migrates_to_v2_and_preserves_created_at(tmp_path: Path) -> None:
    _write_profile(tmp_path, {
        "content": "原文",
        "memory": "原文",
        "metadata": {"created_at": "2026-01-01T00:00:00+08:00", "category": "identity"},
    })

    _store(tmp_path).set_core_profile("原文", {"confidence": 0.9})

    saved = json.loads(_core_path(tmp_path).read_text(encoding="utf-8"))["Sakura"]
    assert saved["schema_version"] == 2
    assert saved["content"] == "原文"
    assert saved["memory"] == "原文"
    assert saved["sections"] == {"legacy": "原文"}
    assert saved["metadata"]["created_at"] == "2026-01-01T00:00:00+08:00"
    assert saved["metadata"]["confidence"] == 0.9


def test_repeated_v2_full_write_keeps_single_legacy_section(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set_core_profile("原文")
    store.set_core_profile("原文")
    saved = _saved_record(tmp_path)
    assert saved["sections"] == {"legacy": "原文"}


def test_full_write_replaces_stale_formal_sections_with_legacy(tmp_path: Path) -> None:
    _write_profile(tmp_path, {
        "schema_version": 2,
        "content": "旧缓存",
        "memory": "旧缓存",
        "sections": {"今の関係": "旧关系", "今の私": "旧自我"},
        "metadata": {},
    })
    _store(tmp_path).set_core_profile("新的整段正文")
    assert _saved_record(tmp_path)["sections"] == {"legacy": "新的整段正文"}


@pytest.mark.parametrize("operation", ["set", "delete"])
def test_unknown_schema_rejects_mutation_without_touching_bytes(tmp_path: Path, operation: str) -> None:
    path = _write_profile(tmp_path, {"schema_version": 99, "content": "未来正文", "metadata": {}})
    before = path.read_bytes()
    store = _store(tmp_path)
    with pytest.raises(CoreProfileStorageError):
        store.set_core_profile("覆盖") if operation == "set" else store.delete_core_profile()
    assert path.read_bytes() == before


@pytest.mark.parametrize("operation", ["set", "delete"])
def test_corrupt_json_rejects_mutation_without_touching_bytes(tmp_path: Path, operation: str) -> None:
    path = _core_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    before = path.read_bytes()
    store = _store(tmp_path)
    with pytest.raises(CoreProfileStorageError):
        store.set_core_profile("覆盖") if operation == "set" else store.delete_core_profile()
    assert path.read_bytes() == before


@pytest.mark.parametrize("operation", ["set", "delete"])
def test_non_object_top_level_rejects_mutation_without_touching_bytes(
    tmp_path: Path,
    operation: str,
) -> None:
    path = _core_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('["not-an-object"]\n', encoding="utf-8")
    before = path.read_bytes()
    store = _store(tmp_path)

    with pytest.raises(CoreProfileStorageError):
        store.set_core_profile("覆盖") if operation == "set" else store.delete_core_profile()

    assert path.read_bytes() == before


@pytest.mark.parametrize("operation", ["set", "delete"])
def test_non_object_scope_record_rejects_mutation_without_touching_bytes(
    tmp_path: Path,
    operation: str,
) -> None:
    path = _core_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"Sakura": "not-an-object"}\n', encoding="utf-8")
    before = path.read_bytes()
    store = _store(tmp_path)

    with pytest.raises(CoreProfileStorageError):
        store.set_core_profile("覆盖") if operation == "set" else store.delete_core_profile()

    assert path.read_bytes() == before


def test_overwrite_creates_backup_of_previous_file(tmp_path: Path) -> None:
    path = _write_profile(tmp_path, {"content": "旧正文", "memory": "旧正文", "metadata": {}})
    before = path.read_bytes()
    _store(tmp_path).set_core_profile("新正文")
    assert path.with_name(path.name + ".bak").read_bytes() == before


def test_first_write_does_not_create_empty_backup(tmp_path: Path) -> None:
    path = _core_path(tmp_path)
    _store(tmp_path).set_core_profile("正文")
    assert path.exists()
    assert not path.with_name(path.name + ".bak").exists()
