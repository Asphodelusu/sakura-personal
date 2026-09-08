"""Global switch and character-profile parsing for relational drive."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from app.core.relational_drive import RelationalDriveProfile

_PROFILE_FIELDS = (
    "physical_baseline",
    "salience_baseline",
    "longing_baseline",
    "afterglow_baseline",
    "inhibition_baseline",
    "physical_half_life_hours",
    "salience_half_life_hours",
    "afterglow_half_life_hours",
    "inhibition_half_life_hours",
    "longing_growth_scale_hours",
    "longing_saturation_hours",
    "appraisal_sensitivity",
    "touch_grace_hours",
    "touch_growth_scale_hours",
    "touch_saturation_hours",
    "touch_hunger_cap",
)


@dataclass(frozen=True)
class RelationshipDriveSettings:
    enabled: bool = True

    def normalized(self) -> "RelationshipDriveSettings":
        return RelationshipDriveSettings(enabled=bool(self.enabled))


def settings_from_mapping(raw: Mapping[str, Any] | None) -> RelationshipDriveSettings:
    source = dict(raw) if isinstance(raw, Mapping) else {}
    return RelationshipDriveSettings(enabled=_as_bool(source.get("enabled"), True)).normalized()


def profile_from_mapping(raw: Any) -> RelationalDriveProfile | None:
    if not isinstance(raw, Mapping) or not dict(raw):
        return None
    natural = RelationalDriveProfile.natural_default()
    values = {field: getattr(natural, field) for field in _PROFILE_FIELDS}
    for field in _PROFILE_FIELDS:
        if field not in raw:
            continue
        parsed = _as_float(raw.get(field))
        if parsed is None:
            continue
        values[field] = parsed
    return RelationalDriveProfile(**values).normalized()


def profile_to_mapping(
    profile: RelationalDriveProfile | None,
    *,
    raw: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if isinstance(raw, Mapping) and dict(raw):
        return dict(raw)
    if profile is None:
        return None
    clean = profile.normalized()
    payload = {field: getattr(clean, field) for field in _PROFILE_FIELDS}
    payload["profile"] = "natural"
    return payload


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number
