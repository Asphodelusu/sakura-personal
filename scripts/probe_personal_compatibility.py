"""Exercise personal-fork compatibility using disposable synthetic data only.

Run from the candidate checkout. Exit 2 means verified compatibility gaps;
unexpected exceptions remain errors and are never converted into a passing gate.
This probe neither starts Core nor imports a real installation.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app" / "plugin_sdk"))
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["ANONYMIZED_TELEMETRY"] = "False"


def probe(work: Path) -> list[dict[str, object]]:
    from app.config.character_loader import _load_profile, load_character_system_prompt
    from app.config.character_studio import _voice_draft_from_manifest
    from app.legacy_import.configuration import add_character_extensions
    from app.legacy_import.errors import LegacyImportError
    from app.legacy_import.incremental import _qdrant_client, _qdrant_points
    from app.storage.paths import StoragePaths
    from app.storage.runtime_roots import RuntimeRoots
    from plugins.builtin.sakura_mem0.boundary import _project_memory
    from plugins.builtin.sakura_mem0.memory import validate_existing_memory_store
    from plugins.builtin.sakura_mem0.support import StoragePaths as MemoryPaths
    from qdrant_client.models import Distance, PointStruct, VectorParams

    results: list[dict[str, object]] = []

    def record(name: str, passed: bool, observed: object) -> None:
        results.append({"requirement": name, "compatible": passed, "observed": observed})

    roots = RuntimeRoots(ROOT, work / "user")
    paths = StoragePaths(roots.user_root)
    memory_paths = MemoryPaths(roots.user_root)
    owned = [memory_paths.memory_dir, memory_paths.memory_cache_dir, paths.logs_dir,
             paths.characters_dir, paths.plugin_dependency_roots_dir,
             paths.runtime_v2_tts_cache_dir, paths.config_dir]
    record("explicit_user_root_contains_default_writes",
           all(path.resolve().is_relative_to(roots.user_root) for path in owned),
           "Default paths only; configured external roots and launched processes are untested.")

    # No embedder or language model: actual local Qdrant storage at both dimensions.
    for dimensions in (384, 1024):
        memory = work / f"memory-{dimensions}"
        client = _qdrant_client(memory / "qdrant")
        try:
            client.create_collection("sakura_memories", vectors_config=VectorParams(
                size=dimensions, distance=Distance.COSINE))
            client.upsert("sakura_memories", [PointStruct(
                id=1, vector=[1.0] + [0.0] * (dimensions - 1),
                payload={"data": "synthetic fact", "user_id": "fixture",
                         "personal": {"sentinel": True}})])
        finally:
            client.close()
        error = None
        try:
            validate_existing_memory_store(memory)
        except ValueError as exc:
            if str(exc) != "memory vector dimensions are incompatible":
                raise
            error = str(exc)
        record(f"existing_{dimensions}_dimension_store_accepted", error is None, error or "accepted")

    # Personal active-generation layout: real storage, not a fabricated metadata file.
    memory = work / "active-layout"
    generation = memory / "indexes" / ("a" * 32)
    client = _qdrant_client(generation / "qdrant")
    try:
        client.create_collection("sakura_memories", vectors_config=VectorParams(
            size=384, distance=Distance.COSINE))
        client.upsert("sakura_memories", [PointStruct(
            id=1, vector=[1.0] + [0.0] * 383,
            payload={"data": "active-generation fact", "user_id": "fixture"})])
        assert client.count("sakura_memories").count == 1
    finally:
        client.close()
    (memory / "active_index.json").write_text(
        json.dumps({"index_id": "a" * 32}), encoding="utf-8")
    try:
        points, reader = _qdrant_points(memory)
    except LegacyImportError as exc:
        if exc.code != "LEGACY_PERSONAL_MEMORY_UNSUPPORTED":
            raise
        record("import_reader_discovers_active_generation", False,
               {"safely_rejected": True, "code": exc.code, "fixture_records": 1})
    else:
        try:
            record("import_reader_discovers_active_generation", len(points) == 1,
                   {"discovered_records": len(points), "fixture_records": 1})
        finally:
            if reader is not None:
                reader.close()

    raw = {"id": "same-id", "content": "synthetic fact", "metadata": {
        "scope": "fixture", "source_entry_ids": ["entry-1"],
        "evidence_kind": "explicit", "personal": {"sentinel": True}}}
    projected = _project_memory(raw, "fixture")
    record("public_memory_projection_preserves_evidence",
           projected is not None and projected.get("sourceEntryIds") == ["entry-1"],
           "Source-entry references checked; arbitrary payload and entity collections need a storage path.")
    foreign = copy.deepcopy(raw)
    foreign["metadata"]["scope"] = "other"
    record("public_memory_projection_filters_foreign_scope",
           _project_memory(foreign, "fixture") is None, "Same record ID in another scope is filtered.")

    staged = work / "character-stage"
    package = staged / "characters" / "fixture"
    package.mkdir(parents=True)
    (package / "card.md").write_text("SYNTHETIC_CARD", encoding="utf-8")
    (package / "guards.md").write_text("SYNTHETIC_GUARD", encoding="utf-8")
    (package / "guide.md").write_text("SYNTHETIC_GUIDE", encoding="utf-8")
    (package / "tones.json").write_text("{}", encoding="utf-8")
    # Loader checks presence only; never load this synthetic model placeholder.
    (package / "old.ckpt").write_text("synthetic placeholder", encoding="utf-8")
    manifest = {"id": "fixture", "display_name": "Synthetic", "card": "card.md",
                "system_guards": "guards.md", "relationship_guide": "guide.md",
                "relationship_drive": {"enabled": True},
                "personal": {"sentinel": [1, "preserve"]},
                "voice": {"tone_refs": "tones.json", "gpt_model": "old.ckpt"}}
    manifest_path = package / "character.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    add_character_extensions(staged)
    converted = json.loads(manifest_path.read_text(encoding="utf-8"))
    record("character_conversion_preserves_personal_fields",
           all(converted.get(key) == manifest[key] for key in
               ("personal", "system_guards", "relationship_guide", "relationship_drive")),
           "File preservation does not prove runtime consumption.")
    profile = _load_profile(manifest_path)
    prompt = load_character_system_prompt(profile)
    record("character_loader_consumes_personal_guards", "SYNTHETIC_GUARD" in prompt,
           {"card_consumed": "SYNTHETIC_CARD" in prompt,
            "guard_consumed": "SYNTHETIC_GUARD" in prompt})
    record("character_profile_exposes_relationship_configuration",
           hasattr(profile, "relationship_guide_path") and hasattr(profile, "relationship_drive_mapping"),
           "Profile fields only; full relationship settlement still requires runtime tests.")
    converted["extensions"]["sakura.tts.gpt-sovits"]["gptModel"] = "ai-edited.ckpt"
    manifest_path.write_text(json.dumps(converted), encoding="utf-8")
    voice = _voice_draft_from_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
    record("canonical_voice_edit_reaches_studio_voice_reader",
           voice is not None and voice.gpt_model == "ai-edited.ckpt",
           {"selected_fixture_model": voice.gpt_model if voice else None})
    return results


def main() -> int:
    parent = ROOT / "temp" / "personal-compatibility"
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=parent, prefix="fixture-") as temporary:
        with patch.object(socket.socket, "connect", side_effect=RuntimeError("Probe network forbidden")):
            results = probe(Path(temporary))
    report = {"scope": "synthetic compatibility probe; no full runtime launched",
              "results": results,
              "blocked": [item["requirement"] for item in results if not item["compatible"]]}
    (parent / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if report["blocked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
