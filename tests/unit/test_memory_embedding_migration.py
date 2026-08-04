from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.agent import memory as memory_module
from app.agent.entity_index import EntityIndex


def test_default_embedding_is_bge_m3_1024() -> None:
    assert memory_module.DEFAULT_EMBEDDING_MODEL == "BAAI/bge-m3"
    assert memory_module.DEFAULT_EMBEDDING_DIMS == 1024
    assert memory_module.DEFAULT_EMBEDDING_MAX_SEQ_LENGTH == 512
    assert "sentencepiece.bpe.model" in memory_module.DEFAULT_EMBEDDING_MODEL_ALLOW_PATTERNS


def test_migrate_rewrites_version_and_exports_backup(tmp_path: Path) -> None:
    memory_dir = tmp_path / "data" / "memory"
    qdrant_path = memory_dir / "qdrant"
    qdrant_path.mkdir(parents=True)
    (qdrant_path / "meta.json").write_text("{}", encoding="utf-8")
    version_file = memory_dir / "embedding_version.txt"
    version_file.write_text("BAAI/bge-base-zh-v1.5:768", encoding="utf-8")

    memory_module._migrate_qdrant_if_needed(
        qdrant_path,
        memory_dir,
        memory_module.DEFAULT_EMBEDDING_DIMS,
        base_dir=tmp_path,
    )

    assert not qdrant_path.exists()
    assert version_file.read_text(encoding="utf-8").strip() == (
        f"{memory_module.DEFAULT_EMBEDDING_MODEL}:{memory_module.DEFAULT_EMBEDDING_DIMS}"
    )


def test_local_embedding_kwargs_include_max_seq_length(tmp_path: Path) -> None:
    kwargs = memory_module._local_embedding_model_kwargs(
        memory_module.DEFAULT_EMBEDDING_MODEL,
        tmp_path,
    )
    assert kwargs["max_seq_length"] == memory_module.DEFAULT_EMBEDDING_MAX_SEQ_LENGTH
    assert "cache_folder" in kwargs


def test_local_path_kwargs_force_offline_without_cache_folder(tmp_path: Path) -> None:
    local_model = tmp_path / "snapshot"
    local_model.mkdir()
    kwargs = memory_module._local_embedding_model_kwargs(
        memory_module.DEFAULT_EMBEDDING_MODEL,
        tmp_path,
        model_name_or_path=str(local_model),
    )
    assert kwargs["local_files_only"] is True
    assert "cache_folder" not in kwargs


def test_ensure_embedding_model_ready_does_not_auto_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Path | None] = []

    def fake_download(base_dir: Path | None = None) -> object:
        calls.append(base_dir)
        return object()

    monkeypatch.setattr(memory_module, "_embedding_model_cached", lambda *_a, **_k: False)
    monkeypatch.setattr(memory_module, "download_embedding_model", fake_download)
    with pytest.raises(memory_module.MemoryModelImportError, match="不会自动下载"):
        memory_module._ensure_embedding_model_ready(tmp_path)
    assert calls == []


def test_import_backup_memories_uses_infer_false(tmp_path: Path) -> None:
    memory_dir = tmp_path / "data" / "memory"
    memory_dir.mkdir(parents=True)
    backup = memory_dir / "memory_backup.json"
    backup.write_text(
        json.dumps(
            [
                {
                    "content": "对方喜欢抹茶",
                    "scope": "Sakura",
                    "layer": "semantic",
                    "importance": 0.8,
                    "confidence": 0.9,
                    "source": "migrated",
                    "metadata": {},
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    mem = MagicMock()
    mem.add.return_value = {
        "results": [{"id": "new-id-1", "memory": "对方喜欢抹茶", "user_id": "Sakura"}]
    }

    imported = memory_module._import_backup_memories(mem, tmp_path)

    assert mem.add.call_count == 1
    assert mem.add.call_args.kwargs.get("infer") is False
    assert len(imported) == 1
    assert imported[0]["id"] == "new-id-1"
    assert not backup.exists()


def test_entity_index_reset_and_rebuild(tmp_path: Path) -> None:
    index = EntityIndex(tmp_path / "entity_index.db")
    # 片假名实体抽取最稳，避免中文正则切碎导致断言抖动
    index.index_memory("old-id", "ソフィアが来た", updated_at="2026-01-01T00:00:00")
    index.mark_backfilled()
    assert index.is_backfilled()
    assert index.lookup_memory_ids(["ソフィア"]) == ["old-id"]

    index.reset()
    assert not index.is_backfilled()
    assert index.lookup_memory_ids(["ソフィア"]) == []

    store = memory_module.MemoryStore(base_dir=tmp_path, memory_client=object())
    store._entity_index = index
    store.rebuild_entity_index(
        [{"id": "new-id", "content": "またソフィアの話をした", "updated_at": "2026-08-04T00:00:00"}]
    )
    assert index.is_backfilled()
    assert index.lookup_memory_ids(["ソフィア"]) == ["new-id"]
    index.close()
