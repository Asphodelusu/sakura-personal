"""Character package optional relationship_drive profile."""

from __future__ import annotations

import json
from pathlib import Path

from app.config.character_loader import CharacterProfile, CharacterRegistry, _load_profile
from app.core.relational_drive import RelationalDriveProfile


def _write_package(root: Path, *, drive: dict | None) -> Path:
    package = root / "characters" / "demo"
    package.mkdir(parents=True)
    (package / "card.md").write_text("card", encoding="utf-8")
    (package / "portrait.png").write_bytes(b"png")
    payload = {
        "id": "demo",
        "display_name": "Demo",
        "card": "card.md",
        "portrait": {"default": "portrait.png"},
    }
    if drive is not None:
        payload["relationship_drive"] = drive
    manifest = package / "character.json"
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return manifest


def test_missing_character_mapping_is_opt_out(tmp_path: Path) -> None:
    profile = _load_profile(_write_package(tmp_path, drive=None))
    assert profile.relationship_drive_profile is None


def test_character_profile_exposes_exact_relational_drive_type() -> None:
    assert CharacterProfile.__annotations__["relationship_drive_profile"] == (
        "RelationalDriveProfile | None"
    )


def test_natural_mapping_loads_profile(tmp_path: Path) -> None:
    profile = _load_profile(_write_package(tmp_path, drive={"profile": "natural"}))
    assert profile.relationship_drive_profile is not None
    assert profile.relationship_drive_profile.physical_half_life_hours == 3.0


def test_live_sakura_profile_loads_approved_natural_parameters() -> None:
    manifest = Path("characters/Sakura/character.json")
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    assert raw["relationship_drive"]["profile"] == "natural"
    registry = CharacterRegistry(Path("."))
    profile = registry.get("Sakura")
    loaded = profile.relationship_drive_profile
    assert loaded is not None
    natural = RelationalDriveProfile.natural_default()
    assert loaded.physical_baseline == 0.10
    assert loaded.salience_baseline == 0.12
    assert loaded.longing_baseline == 0.05
    assert loaded.afterglow_baseline == 0.0
    assert loaded.physical_half_life_hours == 3.0
    assert loaded.salience_half_life_hours == 18.0
    assert loaded.afterglow_half_life_hours == 8.0
    assert loaded.inhibition_half_life_hours == 6.0
    assert loaded.longing_growth_scale_hours == 36.0
    assert loaded.longing_saturation_hours == 72.0
    assert loaded.appraisal_sensitivity == 0.70
    assert loaded.physical_baseline == natural.physical_baseline
