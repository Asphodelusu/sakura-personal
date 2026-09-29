"""Real -I -S Plugin API v4 process, synthetic stores, no daily data."""
import json
import os
from pathlib import Path
import sysconfig
import time
import pytest

from app.plugins.models import PluginSpec
from app.plugins.runtime_v4 import _PluginProcess
from app.storage.paths import StoragePaths
from app.storage.runtime_roots import RuntimeRoots
from test_personal_memory_backend import dependencies
from test_personal_migration_inspection import fixture_copy


@pytest.mark.parametrize("real_model", [False, pytest.param(True, marks=pytest.mark.skipif(
    not os.environ.get("SAKURA_TEST_PERSONAL_SNAPSHOT"), reason="explicit local BGE-M3 snapshot required"))])
def test_personal_worker_initializes_degrades_and_exits(tmp_path, dependencies, monkeypatch, real_model):
    if real_model:
        from test_personal_memory_records import fixture_memory
        from plugins.builtin.sakura_mem0.personal_model import fingerprint_snapshot
        from plugins.builtin.sakura_mem0.personal_records import open_personal_memory_from_snapshot
        from app.legacy_import.personal_copy import prepare_personal_copy
        source = tmp_path / "source"
        root, identity = fixture_memory(source / "data", dependencies, 1024)
        snapshot = Path(os.environ["SAKURA_TEST_PERSONAL_SNAPSHOT"])
        identity["artifact_sha256"] = fingerprint_snapshot(snapshot)
        for path in (root / "active_index.json", root / "indexes" / ("a" * 32) / "journal.json"):
            value = json.loads(path.read_text())
            value["encoding_identity"] = identity
            path.write_text(json.dumps(value))
        store = open_personal_memory_from_snapshot(root, snapshot=snapshot)
        try:
            store.create("alice", "The blue notebook is in the kitchen drawer.", metadata={"source": "explicit"})
        finally:
            store.close()
        work = tmp_path / "work"
        prepare_personal_copy(source, work, source_is_quiescent=lambda: True)
    else:
        _, work, _ = fixture_copy(tmp_path, dependencies)
        snapshot = work / "missing-model"
    repository = Path(__file__).resolve().parents[2]
    plugin_id = "sakura.memory.mem0"
    config = StoragePaths(work).plugin_data_for(plugin_id) / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"personalSnapshot": str(snapshot)}))
    registered = {}
    callbacks = []
    def handler(name, payload):
        if name == "callback.register":
            callbacks.append(payload["shape"])
            return {"handle": "callback-" + str(len(callbacks))}
        if name.startswith("callback."):
            return None
        assert name == "service.call"
        service, method, args = payload["serviceKey"], payload["method"], payload["args"]
        if service == "sakura.host.character":
            return {"id": "alice", "systemPrompt": "synthetic"}
        if service == "sakura.host.storage":
            spec = args[1]
            return {**spec, "path": str(work / spec["scope"] / spec["name"])}
        if method == "register":
            registered[service] = args
            return {"registrationId": service}
        return None
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    worker = _PluginProcess(roots=RuntimeRoots(repository, work), generation_id="personal-process-test",
        spec=PluginSpec(entry="plugin:PersonalRecallPlugin", plugin_id=plugin_id,
                        plugin_root=repository / "plugins/builtin/sakura_mem0"),
        dependency_root=Path(os.environ.get("SAKURA_TEST_PERSONAL_DEPENDENCIES", sysconfig.get_path("purelib"))),
        request_handler=handler, on_exit=lambda *args: None, call_timeout=10)
    try:
        result = worker.start()
        assert result["pid"] != os.getpid()
        assert set(registered) == {"sakura.host.context", "sakura.host.tools"}
        descriptor, handle = registered["sakura.host.tools"]
        assert descriptor["name"] == "memory_search"
        query = {"query": "Where is the blue notebook?"}
        result = worker.invoke_callback(handle, "tools.handler", [query])
        deadline = time.monotonic() + 90
        while result["status"] == "loading" and time.monotonic() < deadline:
            time.sleep(.05)
            result = worker.invoke_callback(handle, "tools.handler", [query])
        if real_model:
            assert result["status"] == "ready" and "blue notebook" in str(result["memories"])
            _, provider = registered["sakura.host.context"]
            assert worker.invoke_callback(provider, "context.contributor", [{"character_id": "alice", "current_input": query["query"]}])
            assert worker.invoke_callback(provider, "context.contributor", [{"character_id": "bob", "current_input": query["query"]}]) == []
        else:
            assert result == {"status": "degraded", "memories": []}
    finally:
        worker.close()
    assert worker.pid is None
