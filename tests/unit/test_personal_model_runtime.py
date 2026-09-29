"""Opt-in real BGE-M3 acceptance; all stores and text are synthetic.

SAKURA_TEST_PERSONAL_SNAPSHOT must name an existing local BGE-M3 snapshot.
Network/LLM/entity NLP are disabled by dependencies; SentenceTransformer is real.
"""
import json
import os
from pathlib import Path
import time

import pytest

from test_personal_memory_backend import dependencies
from test_personal_memory_records import fixture_memory
from plugins.builtin.sakura_mem0.personal_model import fingerprint_snapshot
from plugins.builtin.sakura_mem0.personal_records import open_personal_memory_from_snapshot


@pytest.mark.skipif(not os.environ.get("SAKURA_TEST_PERSONAL_SNAPSHOT"), reason="explicit local BGE-M3 snapshot required")
def test_real_bge_snapshot_scoped_recall_close_reopen_and_inspection(tmp_path, dependencies, monkeypatch):
    from app.legacy_import.personal_copy import prepare_personal_copy
    from app.legacy_import.memory_contract import inspect_personal_migration
    from app.storage.timeline import TimelineStore

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    snapshot = Path(os.environ["SAKURA_TEST_PERSONAL_SNAPSHOT"])
    started = time.monotonic()
    digest = fingerprint_snapshot(snapshot)
    source = tmp_path / "synthetic-source"
    root, identity = fixture_memory(source / "data", dependencies, 1024)
    identity["artifact_sha256"] = digest
    for path in (root / "active_index.json", root / "indexes" / ("a" * 32) / "journal.json"):
        value = json.loads(path.read_text())
        value["encoding_identity"] = identity
        path.write_text(json.dumps(value))
    TimelineStore(source / "data/chat_history/timeline.sqlite3").initialize()
    timings = {}
    for iteration in range(2):
        opened = time.monotonic()
        store = open_personal_memory_from_snapshot(root, snapshot=snapshot)
        timings[f"open_{iteration}_seconds"] = round(time.monotonic() - opened, 3)
        try:
            if iteration == 0:
                expected = store.create("alice", "The blue notebook is stored in the kitchen drawer.")["id"]
                store.create("alice", "Saturn has rings made of ice and rocks.")
            queried = time.monotonic()
            hits = store.search("alice", "Where is the blue notebook?", limit=1)["results"]
            assert hits and hits[0]["id"] == expected
            assert store.search("bob", "Where is the blue notebook?")["results"] == []
            timings[f"queries_{iteration}_seconds"] = round(time.monotonic() - queried, 3)
        finally:
            store.close()
        with pytest.raises(RuntimeError, match="CLOSED"):
            store.search("alice", "notebook")
    work = tmp_path / "synthetic-work"
    prepare_personal_copy(source, work, source_is_quiescent=lambda: True)
    report = inspect_personal_migration(work, snapshot=snapshot)
    assert report["ok"] and report["counts"]["memories"] == 2
    from test_personal_plugin_runtime import Context
    from plugins.builtin.sakura_mem0.plugin import SakuraMem0Plugin
    context = Context(work)
    SakuraMem0Plugin(personal_snapshot=snapshot).setup(context)
    try:
        search = context.tools[0][1]
        deadline = time.monotonic() + 60
        result = search({"query": "Where is the blue notebook?"})
        while result["status"] == "loading" and time.monotonic() < deadline:
            time.sleep(.05)
            result = search({"query": "Where is the blue notebook?"})
        assert result["status"] == "ready" and result["memories"][0]["id"] == expected
        provider = context.providers[0][1]
        assert provider({"character_id": "alice", "current_input": "Where is the blue notebook?"})
        assert provider({"character_id": "bob", "current_input": "Where is the blue notebook?"}) == []
    finally:
        context.effects[-1]()
    assert search({"query": "notebook"})["status"] == "stopped"
    assert fingerprint_snapshot(snapshot) == digest
    timings["total_seconds"] = round(time.monotonic() - started, 3)
    print("REAL_MODEL_ACCEPTANCE " + json.dumps(timings))
