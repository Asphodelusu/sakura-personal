"""Personal candidate contract: a converted character has one voice source."""
import json
import zipfile
from pathlib import Path

import pytest

from app.config.character_archive import (
    export_character_archive, export_character_voice_archive,
    import_character_archive, import_character_voice_archive,
)
from app.config.character_loader import _load_profile, CharacterConfigError
from app.config.character_studio import CharacterStudioService, _voice_draft_from_manifest
from app.core_host.character_settings import CharacterSettingsBoundary

KEY = "sakura.tts.gpt-sovits"


def package(root: Path):
    folder = root / "characters" / "fixture"
    folder.mkdir(parents=True)
    for name, content in {
        "card.md": "synthetic persona", "voice/new.ckpt": "new-gpt",
        "voice/new.pth": "new-sovits", "voice/neutral.wav": "synthetic audio",
        "voice/refs.txt": "voice/neutral.wav|JA|synthetic|neutral\n",
    }.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    data = {"id": "fixture", "display_name": "Fixture", "card": "card.md",
            "visuals": {"resources": []}, "personal": {"preserve": True},
            "extensions": {KEY: {"gptModel": "voice/new.ckpt", "sovitsModel": "voice/new.pth",
                                  "toneRefs": "voice/refs.txt", "refLang": "ja", "textLang": "ja",
                                  "futureOption": "preserve"}, "personal.plugin": {"keep": 1}}}
    write(folder, data)
    return folder, data


def write(folder, data):
    (folder / "character.json").write_text(json.dumps(data), encoding="utf-8")


def test_extension_only_profile_supports_settings_and_voice_export(tmp_path):
    folder, _ = package(tmp_path)
    profile = _load_profile(folder / "character.json")
    assert profile.voice is not None
    assert profile.voice.gpt_model_path.read_text() == "new-gpt"
    assert CharacterSettingsBoundary._has_exportable_voice(profile)
    output = tmp_path / "export.voice"
    export_character_voice_archive(profile, output)
    target, _ = package(tmp_path / "target")
    import_character_voice_archive(output, tmp_path / "target", "fixture")
    updated = json.loads((target / "character.json").read_text())
    assert "voice" not in updated
    assert updated["extensions"][KEY]["futureOption"] == "preserve"
    assert _load_profile(target / "character.json").voice.gpt_model_path.read_text() == "new-gpt"


def test_authoritative_extension_ignores_stale_legacy_paths(tmp_path):
    folder, data = package(tmp_path)
    data["voice"] = {"tone_refs": "missing.txt", "gpt_model": "missing.ckpt"}
    write(folder, data)
    assert _load_profile(folder / "character.json").voice.gpt_model_path.name == "new.ckpt"
    service = CharacterStudioService(tmp_path)
    opened = service.open_character("fixture")
    service.save_character(opened["doc"], opened["workspace_id"])
    saved = json.loads((folder / "character.json").read_text())
    assert "voice" not in saved
    assert saved["personal"] == {"preserve": True}
    assert saved["extensions"]["personal.plugin"] == {"keep": 1}


@pytest.mark.parametrize("value", [None, ""])
def test_clearing_extension_model_never_resurrects_legacy_value(tmp_path, value):
    folder, data = package(tmp_path)
    data["voice"] = {"tone_refs": "voice/refs.txt", "gpt_model": "voice/new.ckpt"}
    if value is None:
        data["extensions"][KEY].pop("gptModel")
    else:
        data["extensions"][KEY]["gptModel"] = value
    write(folder, data)
    assert _voice_draft_from_manifest(data).gpt_model is None
    assert _load_profile(folder / "character.json").voice.gpt_model_path is None


@pytest.mark.parametrize("value", [None, [], "invalid"])
def test_invalid_extension_is_not_masked_by_legacy_voice(tmp_path, value):
    folder, data = package(tmp_path)
    data["voice"] = {"tone_refs": "voice/refs.txt"}
    data["extensions"][KEY] = value
    write(folder, data)
    with pytest.raises(CharacterConfigError):
        _load_profile(folder / "character.json")


def test_character_archive_round_trip_has_one_persistent_voice_source(tmp_path):
    folder, _ = package(tmp_path)
    output = tmp_path / "export.char"
    export_character_archive(_load_profile(folder / "character.json"), output)
    with zipfile.ZipFile(output) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))["character"]
    assert "voice" not in manifest
    assert manifest["extensions"][KEY]["futureOption"] == "preserve"
    imported = import_character_archive(output, tmp_path / "installed")
    profile = _load_profile(imported.package_dir / "character.json")
    assert profile.voice is not None
    assert profile.voice.gpt_model_path.read_text() == "new-gpt"


def test_studio_save_and_remove_voice_never_leave_a_second_source(tmp_path):
    folder, _ = package(tmp_path)
    service = CharacterStudioService(tmp_path)
    opened = service.open_character("fixture")
    service.save_character(opened["doc"], opened["workspace_id"])
    saved = json.loads((folder / "character.json").read_text())
    assert "voice" not in saved
    assert saved["extensions"][KEY]["futureOption"] == "preserve"
    opened = service.open_character("fixture")
    opened["doc"]["voice"] = None
    opened["doc"]["reference_audios"] = []
    service.save_character(opened["doc"], opened["workspace_id"])
    saved = json.loads((folder / "character.json").read_text())
    assert "voice" not in saved
    assert saved["extensions"][KEY] == {"futureOption": "preserve"}
    assert _load_profile(folder / "character.json").voice is None


def test_models_without_reference_config_do_not_disable_character_editing(tmp_path):
    folder, data = package(tmp_path)
    data["extensions"][KEY] = {"gptModel": "voice/new.ckpt"}
    write(folder, data)
    assert _load_profile(folder / "character.json").voice is None
    assert CharacterStudioService(tmp_path).open_character("fixture")["doc"]["voice"]["gpt_model"] == "voice/new.ckpt"


def test_conversion_never_overwrites_existing_extension_even_when_empty(tmp_path):
    from app.legacy_import.configuration import add_character_extensions
    folder, data = package(tmp_path)
    data["voice"] = {"tone_refs": "voice/refs.txt", "gpt_model": "voice/new.ckpt"}
    data["extensions"][KEY] = {}
    write(folder, data)
    add_character_extensions(tmp_path)
    saved = json.loads((folder / "character.json").read_text())
    assert saved["extensions"][KEY] == {}
    assert "voice" not in saved


def test_other_shared_consumers_do_not_resurrect_legacy_models(tmp_path):
    from app.config.plugin_requirements import requirements_for_manifest, GPT_SOVITS_MODELS
    from plugins.builtin.sakura_genie.plugin import _effective_voice_extension
    _, data = package(tmp_path)
    data["voice"] = {"gpt_model": "old.ckpt", "sovits_model": "old.pth"}
    data["extensions"][KEY] = {}
    assert not any(item["type"] == GPT_SOVITS_MODELS for item in requirements_for_manifest(data))
    assert "gptModel" not in _effective_voice_extension(data, {})


def test_card_only_archive_removes_voice_extensions_but_preserves_other_plugins(tmp_path):
    folder, _ = package(tmp_path)
    output = tmp_path / "card.char"
    export_character_archive(_load_profile(folder / "character.json"), output, include_voice=False)
    imported = import_character_archive(output, tmp_path / "installed")
    saved = json.loads((imported.package_dir / "character.json").read_text(encoding="utf-8"))
    assert KEY not in saved["extensions"]
    assert saved["extensions"]["personal.plugin"] == {"keep": 1}
    assert _load_profile(imported.package_dir / "character.json").voice is None


def test_ai_extension_edit_is_seen_by_loader_studio_and_gpt_plugin(tmp_path):
    from plugins.builtin.sakura_gpt_sovits.plugin import _parse_character_voice
    folder, data = package(tmp_path)
    (folder / "voice/edited.ckpt").write_text("edited", encoding="utf-8")
    data["extensions"][KEY]["gptModel"] = "voice/edited.ckpt"
    write(folder, data)

    class Character:
        def resolve_resource(self, character_id, path):
            assert character_id == "fixture"
            return folder / path

    actual = json.loads((folder / "character.json").read_text())
    voice = _parse_character_voice(Character(), "fixture", actual["extensions"][KEY])
    assert voice.gpt_model_path.read_text() == "edited"
    assert _load_profile(folder / "character.json").voice.gpt_model_path == voice.gpt_model_path
    assert _voice_draft_from_manifest(actual).gpt_model == "voice/edited.ckpt"


def test_studio_preserves_unknown_options_without_voice_fields(tmp_path):
    folder, data = package(tmp_path)
    data["extensions"][KEY] = {"futureOption": "preserve"}
    write(folder, data)
    service = CharacterStudioService(tmp_path)
    opened = service.open_character("fixture")
    opened["doc"]["display_name"] = "Renamed"
    service.save_character(opened["doc"], opened["workspace_id"])
    saved = json.loads((folder / "character.json").read_text())
    assert saved["extensions"][KEY] == {"futureOption": "preserve"}


@pytest.mark.parametrize("clear", ["", None])
def test_studio_does_not_reactivate_cleared_reference_config(tmp_path, clear):
    folder, data = package(tmp_path)
    default = folder / "voice/refs/ref.txt"
    default.parent.mkdir(parents=True)
    default.write_text("voice/neutral.wav|JA|stale|neutral\n", encoding="utf-8")
    if clear is None:
        data["extensions"][KEY].pop("toneRefs")
    else:
        data["extensions"][KEY]["toneRefs"] = clear
    write(folder, data)
    service = CharacterStudioService(tmp_path)
    opened = service.open_character("fixture")
    assert opened["doc"]["reference_audios"] == []
    service.save_character(opened["doc"], opened["workspace_id"])
    saved = json.loads((folder / "character.json").read_text())
    assert not saved["extensions"][KEY].get("toneRefs")
    assert _load_profile(folder / "character.json").voice is None
