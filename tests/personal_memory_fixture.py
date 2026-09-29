"""Shared synthetic personal memory for Core and visible desktop rehearsals."""
import json
from pathlib import Path
import shutil
import sys

import pytest


def create_synthetic_memory(source: Path, snapshot: Path) -> None:
    # These helpers live next to the original storage contract tests.
    unit = str(Path(__file__).parent / "unit")
    with pytest.MonkeyPatch.context() as creation:
        creation.syspath_prepend(unit)
        from test_personal_memory_backend import dependencies
        from test_personal_memory_records import fixture_memory
        from plugins.builtin.sakura_mem0.personal_model import fingerprint_snapshot
        from plugins.builtin.sakura_mem0.personal_records import open_personal_memory_from_snapshot

        parts = dependencies.__wrapped__(source, creation)
        memory, identity = fixture_memory(source / "data", parts, 1024)
        identity["artifact_sha256"] = fingerprint_snapshot(snapshot)
        for path in (memory / "active_index.json", memory / "indexes" / ("a" * 32) / "journal.json"):
            value = json.loads(path.read_text())
            value["encoding_identity"] = identity
            path.write_text(json.dumps(value))
        store = open_personal_memory_from_snapshot(memory, snapshot=snapshot)
        try:
            store.create("sakura", "The blue notebook is in the kitchen drawer. CORE_RECALL_312",
                         metadata={"source": "explicit"})
            store.create("other", "The blue notebook belongs to FOREIGN_SCOPE_312.",
                         metadata={"source": "explicit"})
        finally:
            store.close()


def install_personal_plugin(repository: Path, distribution: Path, dependencies: Path) -> None:
    plugin = distribution / "plugins/builtin/sakura_mem0"
    shutil.copytree(repository / "plugins/builtin/sakura_mem0", plugin,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    manifest = plugin / "plugin.yaml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace(
        "plugin:SakuraMem0Plugin", "plugin:PersonalRecallPlugin"), encoding="utf-8")
    dep_root = distribution / "plugins/dependencies/sakura.memory.mem0"
    shutil.copytree(dependencies, dep_root, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (dep_root / ".sakura-dependencies.json").write_text(json.dumps({
        "schemaVersion": 1, "kind": "requirements.txt",
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
    }))
