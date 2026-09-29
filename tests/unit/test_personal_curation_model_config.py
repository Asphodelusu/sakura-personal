"""The old memory_curation slot becomes the memory plugin's curation model selection."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from app.legacy_import.configuration import migrate_configuration

RELATIVE = "data/plugins/sakura.memory.mem0/config.json"


def _source(tmp_path: Path, *, curation: dict | None, models: list[str] | None = None) -> tuple[Path, Path]:
    source, staged = tmp_path / "source", tmp_path / "staged"
    config = source / "data" / "config"
    config.mkdir(parents=True)
    slots = {"chat": {"profile_id": "vision", "model": "big-chat"}}
    if curation is not None:
        slots["memory_curation"] = curation
    api = {
        "api_profiles": [
            {"id": "vision", "alias": "V", "base_url": "https://example.invalid/v1", "api_key": "k1",
             "models": [{"name": "big-chat"}]},
            {"id": "text", "alias": "T", "base_url": "https://example.invalid/v1", "api_key": "k2",
             "models": [{"name": name} for name in (models or ["small-curator"])]},
        ],
        "model_slots": slots,
    }
    (config / "api.yaml").write_text(yaml.safe_dump(api, allow_unicode=True), encoding="utf-8")
    (config / "system_config.yaml").write_text("tool_loop: {}\n", encoding="utf-8")
    return source, staged


def _plugin(staged: Path) -> dict:
    return json.loads((staged / RELATIVE).read_text(encoding="utf-8"))


def test_memory_curation_slot_is_mapped_into_the_plugin(tmp_path: Path) -> None:
    source, staged = _source(tmp_path, curation={"profile_id": "text", "model": "small-curator"})

    migrate_configuration(source, staged, new_tts_root=tmp_path / "tts")

    saved = _plugin(staged)
    assert (saved["curationProfileId"], saved["curationModel"]) == ("text", "small-curator")
    assert "coreMaintainer" in saved


def test_existing_plugin_selection_stays_authoritative(tmp_path: Path) -> None:
    source, staged = _source(tmp_path, curation={"profile_id": "text", "model": "small-curator"})
    current = tmp_path / "current"
    (current / RELATIVE).parent.mkdir(parents=True)
    (current / RELATIVE).write_text(json.dumps({"curationProfileId": "vision", "curationModel": "big-chat"}),
                                    encoding="utf-8")

    migrate_configuration(source, staged, new_tts_root=tmp_path / "tts", existing_user_root=current)

    saved = _plugin(staged)
    assert (saved["curationProfileId"], saved["curationModel"]) == ("vision", "big-chat")


def test_current_selection_wins_even_when_staging_already_has_a_maintainer(tmp_path: Path) -> None:
    source, staged = _source(tmp_path, curation={"profile_id": "text", "model": "small-curator"})
    (staged / RELATIVE).parent.mkdir(parents=True)
    (staged / RELATIVE).write_text(json.dumps({"coreMaintainer": {"enabled": True}}), encoding="utf-8")
    current = tmp_path / "current"
    (current / RELATIVE).parent.mkdir(parents=True)
    (current / RELATIVE).write_text(json.dumps({"curationProfileId": "vision", "curationModel": "big-chat"}),
                                    encoding="utf-8")

    migrate_configuration(source, staged, new_tts_root=tmp_path / "tts", existing_user_root=current)

    saved = _plugin(staged)
    assert (saved["curationProfileId"], saved["curationModel"]) == ("vision", "big-chat")
    assert saved["coreMaintainer"] == {"enabled": True}


def test_unusable_slot_is_not_guessed(tmp_path: Path) -> None:
    source, staged = _source(tmp_path, curation={"profile_id": "text", "model": "missing-model"})

    migrate_configuration(source, staged, new_tts_root=tmp_path / "tts")

    saved = _plugin(staged)
    assert "curationProfileId" not in saved and "curationModel" not in saved


def test_absent_slot_keeps_inheritance(tmp_path: Path) -> None:
    source, staged = _source(tmp_path, curation=None)

    migrate_configuration(source, staged, new_tts_root=tmp_path / "tts")

    saved = _plugin(staged)
    assert "curationProfileId" not in saved and "curationModel" not in saved
