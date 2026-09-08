"""Per-character atomic store for short-term relational drive."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.relational_drive import (
    DriveAppraisal,
    DriveEffect,
    RelationalDriveProfile,
    RelationalDriveState,
    apply_drive_appraisal,
    apply_drive_effect,
    evolve_relational_drive,
)

_SCHEMA_VERSION = 2
_LEDGER_LIMIT = 128


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _format_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _aware(value).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _aware(parsed)


class RelationalDriveStore:
    def __init__(self, path: Path, profile: RelationalDriveProfile) -> None:
        self.path = Path(path)
        self.profile = profile.normalized()
        self._lock = threading.RLock()

    def snapshot(self, now: datetime) -> RelationalDriveState:
        with self._lock:
            state, keys, dirty = self._load(_aware(now))
            evolved = evolve_relational_drive(state, self.profile, now)
            if dirty:
                self._write(evolved, keys)
            return evolved

    def note_contact(self, interaction_id: str, now: datetime) -> RelationalDriveState:
        return self._mutate(interaction_id, "contact", now, apply=None)

    def settle_appraisal(
        self,
        interaction_id: str,
        appraisal: DriveAppraisal,
        now: datetime,
    ) -> bool:
        return self._settle(interaction_id, "appraisal", now, appraisal=appraisal)

    def settle_effect(self, interaction_id: str, effect: DriveEffect, now: datetime) -> bool:
        return self._settle(interaction_id, "effect", now, effect=effect)

    def reset(self, now: datetime) -> RelationalDriveState:
        stamp = _aware(now)
        with self._lock:
            state = RelationalDriveState.from_profile(self.profile, now=stamp)
            self._write(state, [])
            return state

    def _settle(
        self,
        interaction_id: str,
        category: str,
        now: datetime,
        *,
        appraisal: DriveAppraisal | None = None,
        effect: DriveEffect | None = None,
    ) -> bool:
        key = self._ledger_key(interaction_id, category)
        if not key:
            return False
        stamp = _aware(now)
        with self._lock:
            state, keys, _dirty = self._load(stamp)
            if key in keys:
                return False
            evolved = evolve_relational_drive(state, self.profile, stamp)
            if appraisal is not None:
                evolved = apply_drive_appraisal(evolved, self.profile, appraisal, stamp)
            if effect is not None:
                evolved = apply_drive_effect(evolved, self.profile, effect, stamp)
            keys = self._trim_keys([*keys, key])
            self._write(evolved, keys)
            return True

    def _mutate(
        self,
        interaction_id: str,
        category: str,
        now: datetime,
        *,
        apply: None,
    ) -> RelationalDriveState:
        stamp = _aware(now)
        key = self._ledger_key(interaction_id, category)
        with self._lock:
            state, keys, _dirty = self._load(stamp)
            evolved = evolve_relational_drive(state, self.profile, stamp)
            if not key or key in keys:
                return evolved
            if category == "contact":
                evolved = evolved.with_values(last_meaningful_contact_at=stamp, updated_at=stamp)
            keys = self._trim_keys([*keys, key])
            self._write(evolved, keys)
            return evolved

    def _baseline(self, now: datetime) -> RelationalDriveState:
        return RelationalDriveState.from_profile(self.profile, now=now)

    def _load(self, now: datetime) -> tuple[RelationalDriveState, list[str], bool]:
        if not self.path.is_file():
            return self._baseline(now), [], False
        try:
            raw = self.path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, json.JSONDecodeError, UnicodeError):
            self._quarantine()
            restored = self._baseline(now)
            self._write(restored, [])
            return restored, [], True
        if not isinstance(payload, dict):
            self._quarantine()
            restored = self._baseline(now)
            self._write(restored, [])
            return restored, [], True
        updated = _parse_dt(payload.get("updated_at")) or now
        contact = _parse_dt(payload.get("last_meaningful_contact_at"))
        affectionate = _parse_dt(payload.get("last_affectionate_contact_at"))
        try:
            version = int(payload.get("version", 1) or 1)
        except (TypeError, ValueError):
            self._quarantine()
            restored = self._baseline(now)
            self._write(restored, [])
            return restored, [], True
        dirty = False
        if version == 1 and affectionate is None:
            affectionate = now
            dirty = True
        state = RelationalDriveState(
            physical_arousal=payload.get("physical_arousal", self.profile.physical_baseline),
            erotic_salience=payload.get("erotic_salience", self.profile.salience_baseline),
            attachment_longing=payload.get("attachment_longing", self.profile.longing_baseline),
            afterglow=payload.get("afterglow", self.profile.afterglow_baseline),
            inhibition=payload.get("inhibition", self.profile.inhibition_baseline),
            updated_at=updated,
            last_meaningful_contact_at=contact,
            last_affectionate_contact_at=affectionate,
        ).normalized()
        keys = [
            str(item)
            for item in payload.get("settled_keys", [])
            if isinstance(item, str) and item
        ]
        return state, self._trim_keys(keys), dirty

    def _quarantine(self) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        sibling = self.path.with_name(f"{self.path.stem}.corrupt-{stamp}{self.path.suffix}")
        try:
            os.replace(self.path, sibling)
        except OSError:
            try:
                sibling.write_bytes(self.path.read_bytes())
            except OSError:
                return

    def _write(self, state: RelationalDriveState, keys: list[str]) -> None:
        current = state.normalized()
        payload = {
            "version": _SCHEMA_VERSION,
            "updated_at": _format_dt(current.updated_at),
            "last_meaningful_contact_at": _format_dt(current.last_meaningful_contact_at),
            "last_affectionate_contact_at": _format_dt(current.last_affectionate_contact_at),
            "physical_arousal": current.physical_arousal,
            "erotic_salience": current.erotic_salience,
            "attachment_longing": current.attachment_longing,
            "afterglow": current.afterglow,
            "inhibition": current.inhibition,
            "settled_keys": self._trim_keys(keys),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f"{self.path.name}.tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, self.path)
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    @staticmethod
    def _ledger_key(interaction_id: str, category: str) -> str:
        ident = str(interaction_id or "").strip()
        kind = str(category or "").strip()
        if not ident or not kind:
            return ""
        return f"{ident}:{kind}"

    @staticmethod
    def _trim_keys(keys: list[str]) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for item in keys:
            if item in seen:
                continue
            seen.add(item)
            unique.append(item)
        if len(unique) <= _LEDGER_LIMIT:
            return unique
        return unique[-_LEDGER_LIMIT:]
