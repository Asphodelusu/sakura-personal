"""Relationship drive configuration, manifest roundtrip, and key migration."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from app.config.character_loader import CharacterProfile, _load_profile
from app.config.character_studio import CharacterStudioDoc
from app.config.relationship_drive import (
    profile_from_mapping,
    profile_to_mapping,
    settings_from_mapping,
)
from app.core.relational_drive import RelationalDriveProfile
from app.core_host.relationship_settings import load_relationship_turn_settings
from app.legacy_import.configuration import migrate_configuration


def test_drive_settings_and_natural_profile_keep_dev5_defaults() -> None:
    assert settings_from_mapping(None).enabled is True
    assert settings_from_mapping({"enabled": False}).enabled is False
    assert settings_from_mapping({"enabled": "off"}).enabled is False
    assert profile_from_mapping(None) is None
    assert profile_from_mapping({}) is None
    profile = profile_from_mapping({"profile": "natural", "touch_grace_hours": 8})
    assert profile is not None
    assert profile.physical_half_life_hours == 3.0
    assert profile.touch_grace_hours == 8.0
    assert profile.touch_hunger_cap == 0.55
    raw = {"profile": "natural", "custom_future_field": "keep-me"}
    assert profile_to_mapping(profile_from_mapping(raw), raw=raw)["custom_future_field"] == "keep-me"


def test_character_profile_keeps_raw_mapping_and_gates_when_absent(tmp_path: Path) -> None:
    assert CharacterProfile.__annotations__["relationship_drive_profile"] == (
        "RelationalDriveProfile | None"
    )
    missing = _load_profile(_write_package(tmp_path / "missing", drive=None))
    assert missing.relationship_drive_profile is None
    assert missing.relationship_drive_mapping is None

    loaded = _load_profile(
        _write_package(
            tmp_path / "present",
            drive={"profile": "natural", "custom_future_field": "keep-me", "touch_grace_hours": 6},
        )
    )
    assert isinstance(loaded.relationship_drive_profile, RelationalDriveProfile)
    assert loaded.relationship_drive_profile.touch_grace_hours == 6.0
    assert loaded.relationship_drive_mapping is not None
    assert loaded.relationship_drive_mapping["custom_future_field"] == "keep-me"


def test_studio_roundtrip_preserves_unknown_drive_fields(tmp_path: Path) -> None:
    manifest = _write_package(
        tmp_path,
        drive={"profile": "natural", "custom_future_field": "keep-me"},
    )
    doc = CharacterStudioDoc.from_package_dir(manifest.parent)
    assert doc.to_manifest()["relationship_drive"]["custom_future_field"] == "keep-me"
    restored = CharacterStudioDoc.from_payload(doc.to_payload())
    assert restored.to_manifest()["relationship_drive"] == {
        "profile": "natural",
        "custom_future_field": "keep-me",
    }


def test_reader_uses_legacy_keys_without_replacing_canonical_ones(tmp_path: Path) -> None:
    canonical = tmp_path / "config" / "system_config.yaml"
    legacy = tmp_path / "data" / "config" / "system_config.yaml"
    canonical.parent.mkdir(parents=True)
    legacy.parent.mkdir(parents=True)
    canonical.write_text(
        "relationship_drive:\n  enabled: false\nrelationship_initiative:\n  in_turn_enabled: false\n",
        encoding="utf-8",
    )
    legacy.write_text(
        "relationship_drive:\n  enabled: true\nrelationship_initiative:\n  in_turn_enabled: true\n",
        encoding="utf-8",
    )
    drive, in_turn = load_relationship_turn_settings(tmp_path)
    assert drive.enabled is False
    assert in_turn is False

    bare = tmp_path / "bare"
    old = bare / "data" / "config" / "system_config.yaml"
    old.parent.mkdir(parents=True)
    old.write_text(
        "relationship_drive:\n  enabled: off\nrelationship_initiative:\n  in_turn_enabled: 'no'\n",
        encoding="utf-8",
    )
    migrated_drive, migrated_turn = load_relationship_turn_settings(bare)
    assert migrated_drive.enabled is False
    assert migrated_turn is False
    assert load_relationship_turn_settings(tmp_path / "empty")[1] is True


def test_import_copies_relationship_keys_and_keeps_explicit_canonical(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    legacy = source / "data" / "config" / "system_config.yaml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        yaml.safe_dump(
            {
                "relationship_drive": {"enabled": True, "legacy_only": 1},
                "relationship_initiative": {"in_turn_enabled": True, "expression_bias": "natural"},
                "tool_loop": {"max_agent_steps_per_turn": 4},
            }
        ),
        encoding="utf-8",
    )
    existing = tmp_path / "existing"
    (existing / "config").mkdir(parents=True)
    (existing / "config" / "system_config.yaml").write_text(
        "relationship_drive:\n  enabled: false\n",
        encoding="utf-8",
    )
    staged = tmp_path / "staged"
    migrate_configuration(
        source,
        staged,
        new_tts_root=tmp_path / "tts",
        existing_user_root=existing,
    )
    written = yaml.safe_load((staged / "config" / "system_config.yaml").read_text(encoding="utf-8"))
    assert written["relationship_drive"] == {"enabled": False}
    assert written["relationship_initiative"]["in_turn_enabled"] is True
    assert written["relationship_initiative"]["expression_bias"] == "natural"
    assert written["tool_loop"]["max_agent_steps_per_turn"] == 4


def _write_package(root: Path, *, drive: dict | None) -> Path:
    package = root / "characters" / "demo"
    package.mkdir(parents=True)
    (package / "card.md").write_text("card", encoding="utf-8")
    payload = {
        "id": "demo",
        "display_name": "Demo",
        "card": "card.md",
    }
    if drive is not None:
        payload["relationship_drive"] = drive
    manifest = package / "character.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest
