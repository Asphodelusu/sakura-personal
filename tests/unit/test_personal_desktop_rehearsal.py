import json
from contextlib import contextmanager
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from tools.personal_desktop_rehearsal import prepare, start_provider, stop_provider


def test_temporary_api_is_accepted_by_real_inner_thought_resolver(tmp_path):
    import yaml
    from tools.personal_desktop_rehearsal import temporary_live_api
    from app.core_host.inner_thought_settings import _load_choice
    source = tmp_path / 'source.yaml'
    target = tmp_path / 'config/api.yaml'
    target.parent.mkdir()
    source.write_text(yaml.safe_dump({'api_profiles': [
        {'id': 'shared', 'base_url': 'https://example.invalid/v1', 'api_key': 'synthetic'}],
        'model_slots': {'chat': {'profile_id': 'shared', 'model': 'chat'},
                        'inner_thought': {'profile_id': 'shared', 'model': 'thought'}}}), encoding='utf-8')
    with temporary_live_api(source, target):
        choice = _load_choice(tmp_path)
        assert choice.settings is not None
        assert choice.source_slot == 'inner_thought'
        assert choice.settings.model == 'thought'
    assert not target.exists()


@pytest.mark.parametrize('selection', [
    {'profile_id': 'aux', 'model': 'thought'},
    {},
    {'profile_id': '', 'model': ''},
])
def test_live_api_preserves_optional_thought_slots_and_restores(tmp_path, selection):
    import yaml
    from tools.personal_desktop_rehearsal import temporary_live_api
    source, target = tmp_path / 'source.yaml', tmp_path / 'target.yaml'
    source.write_text(yaml.safe_dump({'api_profiles': [
        {'id': 'main', 'base_url': 'https://example.invalid', 'api_key': 'fake'},
        {'id': 'aux', 'base_url': 'https://aux.invalid', 'api_key': 'fake-aux'}],
        'model_slots': {'chat': {'profile_id': 'main', 'model': 'chat'},
                        'inner_thought': selection,
                        'chat_fast': {'profile_id': 'aux', 'model': 'fast'}}}), encoding='utf-8')
    target.write_bytes(b'ORIGINAL')
    with temporary_live_api(source, target):
        slots = yaml.safe_load(target.read_text(encoding='utf-8'))['model_slots']
        assert slots['chat_fast']['model'] == 'fast'
        if selection.get('model'):
            assert slots['inner_thought'] == selection
        else:
            assert 'inner_thought' not in slots
    assert target.read_bytes() == b'ORIGINAL'


@pytest.mark.parametrize('selection', [{'profile_id': 'aux'}, {'model': 'thought'}, 42])
def test_live_api_rejects_invalid_explicit_thought_without_writing(tmp_path, selection):
    import yaml
    from tools.personal_desktop_rehearsal import temporary_live_api
    source, target = tmp_path / 'source.yaml', tmp_path / 'target.yaml'
    source.write_text(yaml.safe_dump({'api_profiles': [
        {'id': 'main', 'base_url': 'https://example.invalid', 'api_key': 'fake'}],
        'model_slots': {'chat': {'profile_id': 'main', 'model': 'chat'},
                        'inner_thought': selection}}), encoding='utf-8')
    target.write_bytes(b'ORIGINAL')
    with pytest.raises(ValueError, match='inner_thought'):
        with temporary_live_api(source, target):
            pytest.fail('invalid optional selection accepted')
    assert target.read_bytes() == b'ORIGINAL'


def test_writable_live_api_uses_explicit_plugin_curation_selection_and_restores(tmp_path):
    import yaml
    from tools.personal_desktop_rehearsal import temporary_live_api
    source, target = tmp_path / 'source.yaml', tmp_path / 'target.yaml'
    source.write_text(yaml.safe_dump({'api_profiles': [
        {'id': 'shared', 'base_url': 'https://example.invalid/v1', 'api_key': 'synthetic'},
        {'id': 'curation', 'base_url': 'https://curation.invalid/v1', 'api_key': 'curation-key'}],
        'model_slots': {'chat': {'profile_id': 'shared', 'model': 'chat-model'},
                        'memory_curation': {'profile_id': 'shared', 'model': 'wrong-model'}}}))
    target.write_text('LOCAL_ONLY')
    with temporary_live_api(source, target,
                            curation_selection={'profile_id': 'curation', 'model': 'curator'}):
        value = yaml.safe_load(target.read_text())
        assert set(value['model_slots']) == {'chat', 'memory_curation'}
        assert value['model_slots']['memory_curation'] == {
            'profile_id': 'curation', 'model': 'curator'}
        assert [profile['id'] for profile in value['api_profiles']] == ['shared', 'curation']
        assert value['api_profiles'][1]['models'] == [{'name': 'curator'}]
    assert target.read_text() == 'LOCAL_ONLY'


def test_writable_live_api_empty_curation_selection_inherits_chat(tmp_path):
    import yaml
    from tools.personal_desktop_rehearsal import temporary_live_api
    source, target = tmp_path / 'source.yaml', tmp_path / 'target.yaml'
    source.write_text(yaml.safe_dump({'api_profiles': [
        {'id': 'shared', 'base_url': 'https://example.invalid/v1', 'api_key': 'synthetic'}],
        'model_slots': {'chat': {'profile_id': 'shared', 'model': 'chat-model'}}}))
    with temporary_live_api(source, target, curation_selection={}):
        value = yaml.safe_load(target.read_text())
        assert value['model_slots'] == {
            'chat': {'profile_id': 'shared', 'model': 'chat-model'}}
        assert value['api_profiles'][0]['models'] == [{'name': 'chat-model'}]
    assert not target.exists()


@pytest.mark.parametrize(('entry', 'write_rehearsal'), [
    ('plugin:PersonalWriteRehearsalPlugin', False),
    ('plugin:PersonalRecallPlugin', True),
])
def test_writable_manifest_and_write_flag_must_match_before_launch(
        tmp_path, entry, write_rehearsal):
    from tools.personal_desktop_rehearsal import MARKER, run
    target, user, distribution, cache = (tmp_path / name for name in ('trial', 'work', 'dist', 'bm25'))
    for path in (target, user / 'config', distribution / 'plugins/builtin/sakura_mem0', cache):
        path.mkdir(parents=True)
    (user / '.sakura-personal-copy.json').write_text(json.dumps({'state': 'complete', 'role': 'work'}))
    (user / 'config/characters.yaml').write_text('current_character_id: Sakura\n')
    (distribution / 'plugins/builtin/sakura_mem0/plugin.yaml').write_text(
        f'entry: {entry}\n')
    (target / MARKER).write_text(json.dumps({'schema': 1, 'kind': 'personal-copy',
        'user_root': str(user), 'distribution': str(distribution), 'bm25_cache': str(cache)}))

    with pytest.raises(ValueError, match='--write-rehearsal'):
        run(tmp_path, target, write_rehearsal=write_rehearsal)


@pytest.mark.parametrize('role', ['baseline', 'work'])
def test_personal_layout_requires_work_copy_and_preserves_character_id(tmp_path, role):
    from tools.personal_desktop_rehearsal import trial_layout, MARKER
    target, user, distribution, cache = (tmp_path / name for name in ('trial', 'work', 'dist', 'bm25'))
    for path in (target, user, distribution, cache):
        path.mkdir()
    (user / '.sakura-personal-copy.json').write_text(json.dumps({'state': 'complete', 'role': role}))
    (user / 'config').mkdir()
    (user / 'config/characters.yaml').write_text('current_character_id: Sakura\n')
    (target / MARKER).write_text(json.dumps({'schema': 1, 'kind': 'personal-copy',
        'user_root': str(user), 'distribution': str(distribution), 'bm25_cache': str(cache)}))
    if role == 'baseline':
        with pytest.raises(ValueError, match='work copy'):
            trial_layout(target)
    else:
        assert trial_layout(target) == (user.resolve(), distribution.resolve(), 'Sakura', cache.resolve())
        from tools.personal_desktop_rehearsal import run
        with pytest.raises(ValueError, match='explicit live API'):
            run(tmp_path, target)


def test_live_api_uses_only_selected_chat_profile_and_restores_on_failure(tmp_path):
    import yaml
    from tools.personal_desktop_rehearsal import temporary_live_api
    source, target = tmp_path / "source.yaml", tmp_path / "isolated.yaml"
    source.write_text(yaml.safe_dump({
        "api_profiles": [
            {"id": "chosen", "base_url": "https://example.invalid/v1", "api_key": "selected-secret",
             "models": [{"name": "selected-model"}, {"name": "other-model"}]},
            {"id": "other", "api_key": "must-not-copy", "models": []}],
        "model_slots": {"chat": {"profile_id": "chosen", "model": "selected-model"},
                        "memory": {"profile_id": "other", "model": "private"}},
        "tts": {"private": "must-not-copy"},
    }))
    original = source.read_bytes()
    target.write_bytes(b"synthetic original")
    with pytest.raises(RuntimeError, match="simulated exit"):
        with temporary_live_api(source, target):
            value = yaml.safe_load(target.read_text())
            assert len(value["api_profiles"]) == 1
            assert value["api_profiles"][0]["models"] == [{"name": "selected-model"}]
            assert list(value["model_slots"]) == ["chat"]
            assert "must-not-copy" not in target.read_text()
            raise RuntimeError("simulated exit")
    assert target.read_bytes() == b"synthetic original"
    assert source.read_bytes() == original


def test_prepare_refuses_existing_directory_without_touching_contents(tmp_path):
    target = tmp_path / "existing"
    target.mkdir()
    protected = target / "keep.txt"
    protected.write_text("do not replace")
    with pytest.raises(FileExistsError):
        prepare(tmp_path / "repo", target, tmp_path / "model")
    assert protected.read_text() == "do not replace"
    assert list(target.iterdir()) == [protected]


@pytest.mark.parametrize("recalled", [False, True])
def test_local_provider_reports_actual_recalled_context(tmp_path, recalled):
    report = tmp_path / "provider.json"
    server, thread = start_provider(report)
    try:
        assert server.server_address[0] == "127.0.0.1"
        message = "CORE_RECALL_312" if recalled else "synthetic question"
        request = Request(f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                          data=json.dumps({"messages": [{"role": "system", "content": message}]}).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=3) as response:
            result = json.load(response)
        content = json.loads(result["choices"][0]["message"]["content"])
        assert content["segments"][0]["zh"]
        from app.llm.chat_reply import parse_chat_reply_result
        assert parse_chat_reply_result(result["choices"][0]["message"]["content"]).ok
        assert json.loads(report.read_text())["memory_in_request"] is recalled
    finally:
        stop_provider(server, thread)
    assert not thread.is_alive()


class _FakeProcess:
    def __init__(self, code):
        self.code = code
        self.returncode = None
        self.terminated = False

    def wait(self, timeout=None):
        self.returncode = self.code
        return self.code

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = self.code


def _admitted_writable_trial(tmp_path, plugin_config):
    target, user, distribution, cache = (
        tmp_path / name for name in ("trial", "work", "dist", "bm25")
    )
    memory = user / "data/memory"
    for path in (
        target,
        user / "config",
        distribution / "plugins/builtin/sakura_mem0",
        cache,
        memory,
        user / "data/plugins/sakura.memory.mem0",
    ):
        path.mkdir(parents=True)
    user_root = user.resolve()
    memory_root = user_root / "data/memory"
    (user / ".sakura-personal-copy.json").write_text(
        json.dumps({"state": "complete", "role": "work"}), encoding="utf-8")
    (memory / ".sakura-personal-copy.json").write_text(
        json.dumps({"state": "complete"}), encoding="utf-8")
    (memory / ".personal-write-rehearsal.json").write_text(json.dumps({
        "purpose": "personal-memory-write-rehearsal",
        "root": str(memory_root),
    }), encoding="utf-8")
    (user / "config/characters.yaml").write_text(
        "current_character_id: Sakura\n", encoding="utf-8")
    (distribution / "plugins/builtin/sakura_mem0/plugin.yaml").write_text(
        "entry: plugin:PersonalWriteRehearsalPlugin\n", encoding="utf-8")
    (user / "data/plugins/sakura.memory.mem0/config.json").write_text(
        json.dumps(plugin_config), encoding="utf-8")
    from tools.personal_desktop_rehearsal import MARKER
    (target / MARKER).write_text(json.dumps({
        "schema": 1,
        "kind": "personal-copy",
        "user_root": str(user_root),
        "distribution": str(distribution.resolve()),
        "bm25_cache": str(cache.resolve()),
    }), encoding="utf-8")
    api_yaml = user_root / "config/api.yaml"
    original = b"ORIGINAL_API_YAML\n"
    api_yaml.write_bytes(original)
    return {
        "target": target,
        "user": user_root,
        "distribution": distribution.resolve(),
        "api_yaml": api_yaml,
        "original_api": original,
        "repository": tmp_path / "repository",
        "live_api": tmp_path / "live-api.yaml",
        "tts_runtime": tmp_path / "tts-runtime",
    }


def _write_live_api(path, *, include_curation):
    import yaml
    profiles = [{
        "id": "shared",
        "base_url": "https://example.invalid/v1",
        "api_key": "synthetic-chat",
    }]
    if include_curation:
        profiles.append({
            "id": "curation",
            "base_url": "https://curation.invalid/v1",
            "api_key": "synthetic-curation",
        })
    path.write_text(yaml.safe_dump({
        "api_profiles": profiles,
        "model_slots": {"chat": {"profile_id": "shared", "model": "chat-model"}},
    }), encoding="utf-8")


def _patch_launch(monkeypatch, popen):
    from app.core.instance import InstanceAcquireStatus

    voice = {"entered": 0, "exited": 0, "calls": []}

    class _Guard:
        def acquire(self):
            return InstanceAcquireStatus.ACQUIRED

        def release(self):
            return None

    @contextmanager
    def _voice(*args, **kwargs):
        voice["entered"] += 1
        voice["calls"].append((args, kwargs))
        try:
            yield
        finally:
            voice["exited"] += 1

    monkeypatch.setattr("app.core.instance.SingleInstanceGuard", lambda *args, **kwargs: _Guard())
    monkeypatch.setattr("tools.personal_rehearsal_voice.managed_voice", _voice)
    monkeypatch.setattr("tools.personal_desktop_rehearsal.subprocess.Popen", popen)
    return voice


@pytest.mark.parametrize(("plugin_config", "expected_slots"), [
    (
        {"curationProfileId": "curation", "curationModel": "curator"},
        {
            "chat": {"profile_id": "shared", "model": "chat-model"},
            "memory_curation": {"profile_id": "curation", "model": "curator"},
        },
    ),
    (
        {"curationProfileId": "", "curationModel": ""},
        {"chat": {"profile_id": "shared", "model": "chat-model"}},
    ),
])
def test_writable_run_launches_with_private_curation_slot_and_restores(
        tmp_path, monkeypatch, plugin_config, expected_slots):
    import yaml
    from tools.personal_desktop_rehearsal import run
    layout = _admitted_writable_trial(tmp_path, plugin_config)
    _write_live_api(layout["live_api"], include_curation="memory_curation" in expected_slots)
    captured = {}

    def popen(args, cwd=None, env=None):
        captured["args"] = args
        captured["cwd"] = Path(cwd)
        captured["user_root"] = env["SAKURA_RUNTIME_USER_ROOT"]
        captured["api"] = yaml.safe_load(layout["api_yaml"].read_text(encoding="utf-8"))
        return _FakeProcess(17)

    voice = _patch_launch(monkeypatch, popen)

    result = run(
        layout["repository"],
        layout["target"],
        live_api=layout["live_api"],
        tts_runtime=layout["tts_runtime"],
        write_rehearsal=True,
    )

    assert result == 17
    assert captured["args"] == [
        str(layout["repository"] / "desktop/src-tauri/target/debug/sakura.exe")]
    assert captured["cwd"] == layout["distribution"]
    assert captured["user_root"] == str(layout["user"])
    assert captured["api"]["model_slots"] == expected_slots
    assert layout["api_yaml"].read_bytes() == layout["original_api"]
    assert voice["entered"] == 1
    assert voice["exited"] == 1
    assert voice["calls"][0][1]["user_root"] == layout["user"]
    assert voice["calls"][0][1]["character_id"] == "Sakura"


def test_writable_run_startup_failure_restores_api_and_voice(tmp_path, monkeypatch):
    from tools.personal_desktop_rehearsal import run
    layout = _admitted_writable_trial(tmp_path, {
        "curationProfileId": "curation",
        "curationModel": "curator",
    })
    _write_live_api(layout["live_api"], include_curation=True)

    def popen(args, cwd=None, env=None):
        raise RuntimeError("synthetic launch failed")

    voice = _patch_launch(monkeypatch, popen)
    with pytest.raises(RuntimeError, match="synthetic launch failed"):
        run(
            layout["repository"],
            layout["target"],
            live_api=layout["live_api"],
            tts_runtime=layout["tts_runtime"],
            write_rehearsal=True,
        )

    assert layout["api_yaml"].read_bytes() == layout["original_api"]
    assert voice["entered"] == 1
    assert voice["exited"] == 1


@pytest.mark.parametrize("plugin_config", [
    {"curationProfileId": "curation", "curationModel": ""},
    {"curationProfileId": "", "curationModel": "curator"},
    {"curationProfileId": 12, "curationModel": "curator"},
    {"curationProfileId": "curation", "curationModel": ["curator"]},
])
def test_writable_run_rejects_partial_or_non_string_curation_before_launch(
        tmp_path, monkeypatch, plugin_config):
    from tools.personal_desktop_rehearsal import run
    layout = _admitted_writable_trial(tmp_path, plugin_config)
    _write_live_api(layout["live_api"], include_curation=True)
    calls = {"popen": 0}

    def popen(args, cwd=None, env=None):
        calls["popen"] += 1
        raise AssertionError("Popen must not run")

    voice = _patch_launch(monkeypatch, popen)
    with pytest.raises(ValueError, match="invalid private curation model selection"):
        run(
            layout["repository"],
            layout["target"],
            live_api=layout["live_api"],
            tts_runtime=layout["tts_runtime"],
            write_rehearsal=True,
        )

    assert calls["popen"] == 0
    assert voice["entered"] == 0
    assert layout["api_yaml"].read_bytes() == layout["original_api"]
