"""Global relationship_drive switch and profile mapping."""

from __future__ import annotations

from pathlib import Path

from app.config.relationship_drive import (
    RelationshipDriveSettings,
    profile_from_mapping,
    settings_from_mapping,
)
from app.config.settings_service import AppSettingsService
from app.config.yaml_config import load_yaml_mapping, save_yaml_mapping


def test_settings_default_enabled() -> None:
    assert settings_from_mapping(None).enabled is True
    assert settings_from_mapping({"enabled": False}).enabled is False
    assert RelationshipDriveSettings().normalized().enabled is True


def test_missing_mapping_is_opt_out() -> None:
    assert profile_from_mapping(None) is None
    assert profile_from_mapping({}) is None


def test_natural_profile_mapping_uses_defaults() -> None:
    profile = profile_from_mapping({"profile": "natural"})
    assert profile is not None
    assert profile.physical_half_life_hours == 3.0
    assert profile.physical_baseline == 0.10
    assert profile.appraisal_sensitivity == 0.70
    assert profile.touch_grace_hours == 12.0
    assert profile.touch_growth_scale_hours == 48.0
    assert profile.touch_saturation_hours == 120.0
    assert profile.touch_hunger_cap == 0.55


def test_touch_profile_fields_parse_and_normalize() -> None:
    profile = profile_from_mapping(
        {
            "profile": "natural",
            "touch_grace_hours": 6.0,
            "touch_growth_scale_hours": 24.0,
            "touch_saturation_hours": 96.0,
            "touch_hunger_cap": 0.45,
        }
    )
    assert profile is not None
    assert profile.touch_grace_hours == 6.0
    assert profile.touch_growth_scale_hours == 24.0
    assert profile.touch_saturation_hours == 96.0
    assert profile.touch_hunger_cap == 0.45

    invalid = profile_from_mapping(
        {"profile": "natural", "touch_hunger_cap": "nope", "touch_grace_hours": "bad"}
    )
    assert invalid is not None
    assert invalid.touch_hunger_cap == 0.55
    assert invalid.touch_grace_hours == 12.0


def test_profile_to_mapping_includes_touch_fields() -> None:
    from app.config.relationship_drive import profile_to_mapping

    profile = profile_from_mapping({"profile": "natural", "touch_grace_hours": 8.0})
    assert profile is not None
    payload = profile_to_mapping(profile)
    assert payload is not None
    assert payload["touch_grace_hours"] == 8.0
    assert payload["touch_growth_scale_hours"] == 48.0
    assert payload["touch_saturation_hours"] == 120.0
    assert payload["touch_hunger_cap"] == 0.55


def test_profile_to_mapping_preserves_unknown_raw_keys() -> None:
    from app.config.relationship_drive import profile_to_mapping

    raw = {"profile": "natural", "custom_future_field": "keep-me"}
    assert profile_to_mapping(None, raw=raw) == raw


def test_invalid_values_fall_back_only_when_mapping_present() -> None:
    profile = profile_from_mapping({"profile": "natural", "physical_half_life_hours": "nope"})
    assert profile is not None
    assert profile.physical_half_life_hours == 3.0
    assert profile_from_mapping(None) is None


def test_config_modules_do_not_check_sakura_id() -> None:
    core = Path("app/core/relational_drive.py").read_text(encoding="utf-8")
    config = Path("app/config/relationship_drive.py").read_text(encoding="utf-8")
    assert "Sakura" not in core
    assert "Sakura" not in config


def test_settings_save_preserves_siblings(tmp_path: Path) -> None:
    service = AppSettingsService(tmp_path)
    service.system_config_path.parent.mkdir(parents=True, exist_ok=True)
    save_yaml_mapping(
        service.system_config_path,
        {
            "relationship_initiative": {
                "in_turn_enabled": False,
                "expression_bias": "restrained",
            },
            "proactive": {"enabled": False},
            "custom_sibling": {"keep": True},
        },
    )
    service.save_relationship_drive_settings(settings_from_mapping({"enabled": False}))
    data = load_yaml_mapping(service.system_config_path)
    assert data["relationship_drive"]["enabled"] is False
    assert data["relationship_initiative"]["in_turn_enabled"] is False
    assert data["relationship_initiative"]["expression_bias"] == "restrained"
    assert data["proactive"]["enabled"] is False
    assert data["custom_sibling"]["keep"] is True
    assert service.load_relationship_drive_settings().enabled is False
