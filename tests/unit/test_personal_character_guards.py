import json
from pathlib import Path

import pytest

from app.agent.runtime import AgentRuntime
from app.config.character_loader import _load_profile, load_character_system_prompt, CharacterConfigError
from app.config.character_archive import export_character_archive, import_character_archive
from app.core_host.plugin_character import PluginCharacterStore

CARD = "SYNTHETIC_PERSONA"
GUARDS = "## 身份与人称\nSYNTHETIC_IDENTITY\n\n## 边界\nSYNTHETIC_BOUNDARY"


def character(root: Path, *, explicit=True):
    package = root / "characters" / "fixture"
    package.mkdir(parents=True)
    (package / "card.md").write_text(CARD, encoding="utf-8")
    (package / "system_guards.md").write_text(GUARDS, encoding="utf-8")
    manifest = {"id": "fixture", "display_name": "Fixture", "card": "card.md",
                "visuals": {"resources": []}, "personal": {"keep": 1}}
    if explicit:
        manifest["system_guards"] = "system_guards.md"
    (package / "character.json").write_text(json.dumps(manifest), encoding="utf-8")
    config = root / "config"
    config.mkdir()
    (config / "characters.yaml").write_text("current_character_id: fixture\n", encoding="utf-8")
    return package, manifest


@pytest.mark.parametrize("explicit", [True, False])
def test_guard_layers_reach_main_runtime_and_memory_plugin(tmp_path, explicit):
    package, _ = character(tmp_path, explicit=explicit)
    profile = _load_profile(package / "character.json")
    main = load_character_system_prompt(profile)
    runtime = AgentRuntime(object(), main, character_id="fixture")
    prompt = runtime._static_persona_prompt()
    assert prompt.index("SYNTHETIC_IDENTITY") < prompt.index(CARD) < prompt.index("SYNTHETIC_BOUNDARY")
    assert prompt.count("SYNTHETIC_IDENTITY") == 1
    assert "【身份锚】" in prompt and "【演出约束】" in prompt
    plugin_prompt = PluginCharacterStore(tmp_path).current("sakura.memory.mem0")["systemPrompt"]
    assert plugin_prompt == main


def test_ai_guard_edit_is_reloaded_and_survives_archive(tmp_path):
    package, _ = character(tmp_path)
    store = PluginCharacterStore(tmp_path)
    before = store.current("fixture.plugin")["systemPrompt"]
    (package / "system_guards.md").write_text(GUARDS.replace("SYNTHETIC_BOUNDARY", "UPDATED_BOUNDARY"), encoding="utf-8")
    after = store.current("fixture.plugin")["systemPrompt"]
    assert before != after and "UPDATED_BOUNDARY" in after
    output = tmp_path / "character.char"
    export_character_archive(_load_profile(package / "character.json"), output, include_voice=False)
    result = import_character_archive(output, tmp_path / "installed")
    assert "UPDATED_BOUNDARY" in load_character_system_prompt(_load_profile(result.package_dir / "character.json"))


@pytest.mark.parametrize("path", ["absent.md", "../outside.md", "../../outside.md"])
def test_explicit_guard_cannot_disappear_or_escape_silently(tmp_path, path):
    package, manifest = character(tmp_path)
    manifest["system_guards"] = path
    (tmp_path / "characters/outside.md").write_text("not owned", encoding="utf-8")
    (tmp_path / "outside.md").write_text("not owned", encoding="utf-8")
    (package / "character.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(CharacterConfigError):
        _load_profile(package / "character.json")


def test_guard_disappearing_after_profile_load_fails_instead_of_changing_persona(tmp_path):
    package, _ = character(tmp_path)
    profile = _load_profile(package / "character.json")
    (package / "system_guards.md").unlink()
    with pytest.raises(CharacterConfigError):
        load_character_system_prompt(profile)


def test_unconfigured_character_keeps_upstream_prompt_contract(tmp_path):
    package, _ = character(tmp_path, explicit=False)
    (package / "system_guards.md").unlink()
    profile = _load_profile(package / "character.json")
    assert "【身份锚】" not in load_character_system_prompt(profile)
    assert PluginCharacterStore(tmp_path).current("fixture.plugin")["systemPrompt"] == CARD
