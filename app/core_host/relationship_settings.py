"""Read relational-drive switches from the canonical user config.

Only ``relationship_drive`` and ``relationship_initiative.in_turn_enabled`` are
used. Proactive timers stay out of this slice. A key already present in
``config/system_config.yaml`` is not replaced by the old ``data/config`` copy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from app.config.relationship_drive import RelationshipDriveSettings, settings_from_mapping
from app.config.yaml_config import load_yaml_mapping


def load_relationship_turn_settings(user_root: Path) -> tuple[RelationshipDriveSettings, bool]:
    root = Path(user_root)
    canonical = _read_mapping(root / "config" / "system_config.yaml")
    legacy: dict[str, Any] | None = None

    def section(name: str) -> Any:
        nonlocal legacy
        if name in canonical:
            return canonical.get(name)
        if legacy is None:
            legacy = _read_mapping(root / "data" / "config" / "system_config.yaml")
        if name in legacy:
            return legacy.get(name)
        return None

    drive_raw = section("relationship_drive")
    drive = settings_from_mapping(drive_raw if isinstance(drive_raw, Mapping) else None)
    initiative_raw = section("relationship_initiative")
    in_turn_enabled = True
    if isinstance(initiative_raw, Mapping) and "in_turn_enabled" in initiative_raw:
        in_turn_enabled = _as_bool(initiative_raw.get("in_turn_enabled"), True)
    return drive, in_turn_enabled


def _read_mapping(path: Path) -> dict[str, Any]:
    try:
        return load_yaml_mapping(path)
    except (OSError, UnicodeError, ValueError):
        return {}


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default
