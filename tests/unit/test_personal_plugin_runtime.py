import json
import os
import threading
import time
import pytest
from types import SimpleNamespace

from app.legacy_import.personal_copy import prepare_personal_copy
from plugins.builtin.sakura_mem0.plugin import SakuraMem0Plugin
from test_personal_memory_backend import dependencies
from test_personal_model_loading import model_fixture


class Context:
    def __init__(self, root):
        self.root, self.effects, self.providers, self.tools = root, [], [], []
        self.settings, self.surfaces, self.collections = [], [], []
    def data_path(self, relative):
        return self.root / "data/plugins/mem0" / relative
    def effect(self, callback):
        self.effects.append(callback)
    def get(self, name):
        if name == "sakura.host.storage":
            return SimpleNamespace(resolve=lambda domain, relative: self.root / domain / relative)
        if name == "sakura.host.character":
            return SimpleNamespace(current=lambda: {"id": "alice"})
        if name == "sakura.host.context":
            return SimpleNamespace(register=lambda *args: self.providers.append(args))
        if name == "sakura.host.tools":
            return SimpleNamespace(register=lambda *args: self.tools.append(args))
        if name == "sakura.host.settings":
            return SimpleNamespace(register=lambda *args, **kwargs: self.settings.append((args, kwargs)))
        if name == "sakura.host.settings.surface-v0":
            return SimpleNamespace(register=lambda *args: self.surfaces.append(args))
        if name == "sakura.host.settings.collection-v0":
            return SimpleNamespace(register=lambda *args, **kwargs: self.collections.append((args, kwargs)))
        raise KeyError(name)


def test_personal_plugin_requires_completed_work_copy(tmp_path, dependencies, monkeypatch):
    model_fixture(tmp_path / "data", dependencies, monkeypatch, 384)
    with pytest.raises(ValueError, match="COPY_REQUIRED"):
        SakuraMem0Plugin(personal_snapshot=tmp_path / "data/snapshot").setup(Context(tmp_path))


def test_plugin_registers_only_recall_and_closes_during_loading(tmp_path, dependencies, monkeypatch):
    from plugins.builtin.sakura_mem0 import personal_records
    source = tmp_path / "source"
    model_fixture(source / "data", dependencies, monkeypatch, 384)
    work = tmp_path / "work"
    prepare_personal_copy(source, work, source_is_quiescent=lambda: True)
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    def opening(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(close=closed.set)
    monkeypatch.setattr(personal_records, "open_personal_memory_from_snapshot", opening)
    context = Context(work)
    SakuraMem0Plugin(personal_snapshot=work / "data/snapshot").setup(context)
    assert entered.wait(2)
    assert len(context.providers) == 1
    assert [descriptor["name"] for descriptor, _ in context.tools] == ["memory_search"]
    search = context.tools[0][1]
    assert search({"query": "fact"})["status"] == "loading"
    stopping = threading.Thread(target=context.effects[-1])
    stopping.start()
    release.set()
    stopping.join(5)
    assert not stopping.is_alive() and closed.is_set()
    assert search({"query": "fact"})["status"] == "stopped"


def test_plugin_context_and_search_use_personal_records(tmp_path, dependencies, monkeypatch):
    from plugins.builtin.sakura_mem0.personal_records import open_personal_memory_from_snapshot
    source = tmp_path / "source"
    root, snapshot, _, _ = model_fixture(source / "data", dependencies, monkeypatch, 384)
    store = open_personal_memory_from_snapshot(root, snapshot=snapshot)
    try:
        store.create("alice", "synthetic remembered fact", metadata={"source": "explicit"})
        store.create("bob", "foreign private fact")
    finally:
        store.close()
    work = tmp_path / "work"
    prepare_personal_copy(source, work, source_is_quiescent=lambda: True)
    context = Context(work)
    SakuraMem0Plugin(personal_snapshot=work / "data/snapshot").setup(context)
    try:
        search = context.tools[0][1]
        deadline = time.monotonic() + 5
        result = search({"query": "fact"})
        while result["status"] == "loading" and time.monotonic() < deadline:
            time.sleep(.01)
            result = search({"query": "fact"})
        assert result["status"] == "ready"
        assert [item["content"] for item in result["memories"]] == ["synthetic remembered fact"]
        provider = context.providers[0][1]
        fragments = provider({"character_id": "alice", "current_input": "fact"})
        assert fragments and "synthetic remembered fact" in str(fragments)
        assert provider({"character_id": "bob", "current_input": "fact"}) == []
    finally:
        context.effects[-1]()


def test_failed_model_loading_degrades_provider_without_fake_hits(tmp_path, dependencies, monkeypatch):
    from plugins.builtin.sakura_mem0 import personal_records
    from plugins.builtin.sakura_mem0 import personal_runtime
    diagnostics = []
    monkeypatch.setattr(personal_runtime, 'log_event', lambda *args, **kwargs: diagnostics.append((args, kwargs)))
    source = tmp_path / "source"
    model_fixture(source / "data", dependencies, monkeypatch, 384)
    work = tmp_path / "work"
    prepare_personal_copy(source, work, source_is_quiescent=lambda: True)
    def fail(*args, **kwargs):
        raise ValueError("synthetic failure")
    monkeypatch.setattr(personal_records, "open_personal_memory_from_snapshot", fail)
    context = Context(work)
    SakuraMem0Plugin(personal_snapshot=work / "data/snapshot").setup(context)
    try:
        search = context.tools[0][1]
        deadline = time.monotonic() + 5
        result = search({"query": "fact"})
        while result["status"] == "loading" and time.monotonic() < deadline:
            time.sleep(.01)
            result = search({"query": "fact"})
        assert result == {"status": "degraded", "memories": []}
        assert any(item[1].get('event') == 'memory.personal.load_failed' for item in diagnostics)
        assert context.providers[0][1]({"character_id": "alice", "current_input": "fact"}) == []
    finally:
        context.effects[-1]()


def _copy_root(tmp_path):
    root = tmp_path / "work"
    memory = root / "data" / "memory"
    memory.mkdir(parents=True)
    (root / ".sakura-personal-copy.json").write_text(
        json.dumps({"state": "complete"}), encoding="utf-8"
    )
    return root, memory


def _open_plugin(tmp_path, monkeypatch, opener):
    from plugins.builtin.sakura_mem0 import personal_records
    root, memory = _copy_root(tmp_path)
    monkeypatch.setattr(personal_records, "open_personal_memory_from_snapshot", opener)
    context = Context(root)
    SakuraMem0Plugin(personal_snapshot=root / "data" / "snapshot").setup(context)
    return context, memory


def _wait_status(context, status):
    search = context.tools[0][1]
    deadline = time.monotonic() + 5
    result = search({"query": "fact"})
    while result["status"] == "loading" and time.monotonic() < deadline:
        time.sleep(.01)
        result = search({"query": "fact"})
    assert result["status"] == status
    return result


def _profile(memory, payload):
    path = memory / "core_profiles.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class _Facts:
    def search(self, scope, query, limit=10):
        return {"results": [
            {"id": "m1", "memory": "synthetic remembered fact", "user_id": scope,
             "score": 0.9, "metadata": {"source": "explicit"}},
            {"id": "core_profile:alice", "memory": "always on profile", "user_id": scope,
             "score": 0.9, "metadata": {"source": "explicit"}},
        ]}

    def close(self):
        pass


def test_context_keeps_profile_without_query_or_ready_vectors(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def opening(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        raise RuntimeError("synthetic model missing")

    context, memory = _open_plugin(tmp_path, monkeypatch, opening)
    _profile(memory, {"alice": {"content": "always on profile"}, "bob": {"content": "bob private"}})
    try:
        assert entered.wait(2)
        provider = context.providers[0][1]
        loading = provider({"character_id": "alice", "current_input": ""})
        assert loading[0]["id"] == "core_profile:alice"
        assert loading[0]["content"] == "【常驻档案】\nalways on profile"
        assert loading[0]["sensitivity"] == "private"
        assert "bob private" not in str(loading)
        assert [descriptor["name"] for descriptor, _ in context.tools] == ["memory_search"]
        release.set()
        _wait_status(context, "degraded")
        degraded = provider({"character_id": "alice", "current_input": "fact"})
        assert degraded[0]["content"] == "【常驻档案】\nalways on profile"
        assert provider({"character_id": "bob", "current_input": "fact"}) == []
        context.effects[-1]()
        assert provider({"character_id": "alice", "current_input": ""}) == []
    finally:
        release.set()
        if context.effects:
            context.effects[-1]()


def test_context_prepends_profile_and_keeps_vector_hits(tmp_path, monkeypatch):
    context, memory = _open_plugin(tmp_path, monkeypatch, lambda *args, **kwargs: _Facts())
    _profile(memory, {"alice": {"content": "always on profile"}})
    try:
        _wait_status(context, "ready")
        fragments = context.providers[0][1]({"character_id": "alice", "current_input": "fact"})
        assert fragments[0]["id"] == "core_profile:alice"
        assert "always on profile" in fragments[0]["content"]
        assert any("synthetic remembered fact" in item["content"] for item in fragments[1:])
        assert sum("always on profile" in item["content"] for item in fragments) == 1
        assert all(item["id"] != "memory.core_profile:alice" for item in fragments)
    finally:
        context.effects[-1]()


def test_corrupt_profile_does_not_block_or_rewrite_recall(tmp_path, monkeypatch):
    import plugins.builtin.sakura_mem0.personal_core_profile as reader
    diagnostics = []
    monkeypatch.setattr(reader, "log_event", lambda *args, **kwargs: diagnostics.append((args, kwargs)))
    context, memory = _open_plugin(tmp_path, monkeypatch, lambda *args, **kwargs: _Facts())
    path = memory / "core_profiles.json"
    raw = "损坏的档案正文".encode() + b"\xff"
    path.write_bytes(raw)
    try:
        _wait_status(context, "ready")
        fragments = context.providers[0][1]({"character_id": "alice", "current_input": "fact"})
        assert any("synthetic remembered fact" in item["content"] for item in fragments)
        assert all("损坏的档案正文" not in item["content"] for item in fragments)
        assert path.read_bytes() == raw
        assert diagnostics[0][0][2]["code"] == "CORE_PROFILE_ENCODING"
        assert "损坏的档案正文" not in str(diagnostics)
    finally:
        context.effects[-1]()


def test_substituted_profile_link_is_absent_after_startup(tmp_path, monkeypatch):
    context, memory = _open_plugin(tmp_path, monkeypatch, lambda *args, **kwargs: _Facts())
    path = _profile(memory, {"alice": {"content": "owned profile"}})
    try:
        _wait_status(context, "ready")
        assert "owned profile" in str(context.providers[0][1]({"character_id": "alice", "current_input": ""}))
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps({"alice": {"content": "escaped profile"}}), encoding="utf-8")
        path.unlink()
        os.link(outside, path)
        fragments = context.providers[0][1]({"character_id": "alice", "current_input": ""})
        assert "escaped profile" not in str(fragments)
        assert "owned profile" not in str(fragments)
    finally:
        context.effects[-1]()
