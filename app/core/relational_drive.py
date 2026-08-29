"""Character-agnostic short-term relational drive: numbers, decay, bands."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping

_UNIT_MIN = 0.0
_UNIT_MAX = 1.0
_HALF_LIFE_MIN_HOURS = 0.25
_HALF_LIFE_MAX_HOURS = 720.0
_APPRAISAL_SUBTLE = 0.025
_APPRAISAL_MILD = 0.060
_APPRAISAL_STRONG = 0.100
_BANDS = ("quiet", "warm", "drawn", "strong")
_BAND_THRESHOLDS = (0.25, 0.55, 0.78)
_BAND_MARGIN = 0.05

_EFFECT_VECTORS: dict[str, dict[str, float]] = {
    "none": {},
    "mutual_affection": {
        "physical_arousal": 0.03,
        "erotic_salience": 0.04,
        "attachment_longing": -0.04,
        "afterglow": 0.02,
    },
    "mutual_escalation": {
        "physical_arousal": 0.12,
        "erotic_salience": 0.08,
        "attachment_longing": -0.05,
    },
    "fulfilled": {
        "physical_arousal": -0.45,
        "erotic_salience": -0.10,
        "attachment_longing": -0.15,
        "afterglow": 0.55,
        "inhibition": -0.05,
    },
    "aftercare": {
        "physical_arousal": -0.10,
        "afterglow": 0.12,
        "inhibition": -0.03,
    },
    "hesitation": {
        "physical_arousal": -0.03,
        "inhibition": 0.15,
    },
    "stopped": {
        "physical_arousal": -0.08,
        "inhibition": 0.25,
    },
}

_KIND_FIELDS = {
    "physical_arousal": "physical_arousal",
    "erotic_salience": "erotic_salience",
    "attachment_longing": "attachment_longing",
    "afterglow": "afterglow",
    "inhibition": "inhibition",
}


class DriveKind(StrEnum):
    PHYSICAL_AROUSAL = "physical_arousal"
    EROTIC_SALIENCE = "erotic_salience"
    ATTACHMENT_LONGING = "attachment_longing"
    AFTERGLOW = "afterglow"
    INHIBITION = "inhibition"


class DriveDirection(StrEnum):
    RISE = "rise"
    FALL = "fall"
    HOLD = "hold"


class DriveStrength(StrEnum):
    SUBTLE = "subtle"
    MILD = "mild"
    STRONG = "strong"


class DriveEffectEvent(StrEnum):
    NONE = "none"
    MUTUAL_AFFECTION = "mutual_affection"
    MUTUAL_ESCALATION = "mutual_escalation"
    FULFILLED = "fulfilled"
    AFTERCARE = "aftercare"
    HESITATION = "hesitation"
    STOPPED = "stopped"


def _finite_or(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _unit(value: Any, default: float) -> float:
    return _clamp(_finite_or(value, default), _UNIT_MIN, _UNIT_MAX)


def _hours(value: Any, default: float) -> float:
    return _clamp(_finite_or(value, default), _HALF_LIFE_MIN_HOURS, _HALF_LIFE_MAX_HOURS)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _elapsed_hours(then: datetime, now: datetime) -> float:
    delta = (_aware(now) - _aware(then)).total_seconds() / 3600.0
    if delta < 0.0:
        return 0.0
    return delta


def _decay_to_baseline(current: float, baseline: float, elapsed_hours: float, half_life: float) -> float:
    if elapsed_hours <= 0.0:
        return current
    factor = 2.0 ** (-elapsed_hours / half_life)
    return baseline + (current - baseline) * factor


def _apply_delta(current: float, delta: float) -> float:
    if delta > 0.0:
        return _clamp(current + delta * (1.0 - current), _UNIT_MIN, _UNIT_MAX)
    return _clamp(current + delta, _UNIT_MIN, _UNIT_MAX)


@dataclass(frozen=True)
class RelationalDriveProfile:
    physical_baseline: float = 0.10
    salience_baseline: float = 0.12
    longing_baseline: float = 0.05
    afterglow_baseline: float = 0.0
    inhibition_baseline: float = 0.0
    physical_half_life_hours: float = 3.0
    salience_half_life_hours: float = 18.0
    afterglow_half_life_hours: float = 8.0
    inhibition_half_life_hours: float = 6.0
    longing_growth_scale_hours: float = 36.0
    longing_saturation_hours: float = 72.0
    appraisal_sensitivity: float = 0.70

    @classmethod
    def natural_default(cls) -> "RelationalDriveProfile":
        return cls().normalized()

    def normalized(self) -> "RelationalDriveProfile":
        return RelationalDriveProfile(
            physical_baseline=_unit(self.physical_baseline, 0.10),
            salience_baseline=_unit(self.salience_baseline, 0.12),
            longing_baseline=_unit(self.longing_baseline, 0.05),
            afterglow_baseline=_unit(self.afterglow_baseline, 0.0),
            inhibition_baseline=_unit(self.inhibition_baseline, 0.0),
            physical_half_life_hours=_hours(self.physical_half_life_hours, 3.0),
            salience_half_life_hours=_hours(self.salience_half_life_hours, 18.0),
            afterglow_half_life_hours=_hours(self.afterglow_half_life_hours, 8.0),
            inhibition_half_life_hours=_hours(self.inhibition_half_life_hours, 6.0),
            longing_growth_scale_hours=_hours(self.longing_growth_scale_hours, 36.0),
            longing_saturation_hours=_hours(self.longing_saturation_hours, 72.0),
            appraisal_sensitivity=_unit(self.appraisal_sensitivity, 0.70),
        )


@dataclass(frozen=True)
class RelationalDriveState:
    physical_arousal: float
    erotic_salience: float
    attachment_longing: float
    afterglow: float
    inhibition: float
    updated_at: datetime
    last_meaningful_contact_at: datetime | None = None

    @classmethod
    def from_profile(
        cls,
        profile: RelationalDriveProfile,
        *,
        now: datetime,
        last_meaningful_contact_at: datetime | None = None,
    ) -> "RelationalDriveState":
        clean = profile.normalized()
        return cls(
            physical_arousal=clean.physical_baseline,
            erotic_salience=clean.salience_baseline,
            attachment_longing=clean.longing_baseline,
            afterglow=clean.afterglow_baseline,
            inhibition=clean.inhibition_baseline,
            updated_at=_aware(now),
            last_meaningful_contact_at=(
                _aware(last_meaningful_contact_at) if last_meaningful_contact_at else None
            ),
        ).normalized()

    def normalized(self) -> "RelationalDriveState":
        contact = self.last_meaningful_contact_at
        return RelationalDriveState(
            physical_arousal=_unit(self.physical_arousal, 0.0),
            erotic_salience=_unit(self.erotic_salience, 0.0),
            attachment_longing=_unit(self.attachment_longing, 0.0),
            afterglow=_unit(self.afterglow, 0.0),
            inhibition=_unit(self.inhibition, 0.0),
            updated_at=_aware(self.updated_at),
            last_meaningful_contact_at=_aware(contact) if contact is not None else None,
        )

    def with_values(self, **changes: Any) -> "RelationalDriveState":
        return replace(self, **changes).normalized()


@dataclass(frozen=True)
class DriveAppraisal:
    kind: DriveKind | str
    direction: DriveDirection | str
    strength: DriveStrength | str


@dataclass(frozen=True)
class DriveEffect:
    event: DriveEffectEvent | str
    strength: DriveStrength | str = DriveStrength.MILD


def evolve_relational_drive(
    state: RelationalDriveState,
    profile: RelationalDriveProfile,
    now: datetime,
) -> RelationalDriveState:
    current = state.normalized()
    clean = profile.normalized()
    stamp = _aware(now)
    elapsed = _elapsed_hours(current.updated_at, stamp)
    if elapsed <= 0.0 and stamp < _aware(current.updated_at):
        return current
    physical = _decay_to_baseline(
        current.physical_arousal,
        clean.physical_baseline,
        elapsed,
        clean.physical_half_life_hours,
    )
    salience = _decay_to_baseline(
        current.erotic_salience,
        clean.salience_baseline,
        elapsed,
        clean.salience_half_life_hours,
    )
    afterglow = _decay_to_baseline(
        current.afterglow,
        clean.afterglow_baseline,
        elapsed,
        clean.afterglow_half_life_hours,
    )
    inhibition = _decay_to_baseline(
        current.inhibition,
        clean.inhibition_baseline,
        elapsed,
        clean.inhibition_half_life_hours,
    )
    longing = current.attachment_longing
    if current.last_meaningful_contact_at is not None:
        absence = _elapsed_hours(current.last_meaningful_contact_at, stamp)
        if absence > 0.0:
            bounded_absence = min(absence, clean.longing_saturation_hours)
            grown = 1.0 - math.exp(-bounded_absence / clean.longing_growth_scale_hours)
            longing = max(longing, grown)
    return RelationalDriveState(
        physical_arousal=physical,
        erotic_salience=salience,
        attachment_longing=longing,
        afterglow=afterglow,
        inhibition=inhibition,
        updated_at=stamp if elapsed > 0.0 else current.updated_at,
        last_meaningful_contact_at=current.last_meaningful_contact_at,
    ).normalized()


def _appraisal_magnitude(strength: str, sensitivity: float) -> float:
    if strength == DriveStrength.SUBTLE:
        return _APPRAISAL_SUBTLE * sensitivity
    if strength == DriveStrength.MILD:
        return _APPRAISAL_MILD * sensitivity
    if strength == DriveStrength.STRONG:
        return _APPRAISAL_STRONG * sensitivity
    return 0.0


def apply_drive_appraisal(
    state: RelationalDriveState,
    profile: RelationalDriveProfile,
    appraisal: DriveAppraisal,
    now: datetime,
) -> RelationalDriveState:
    evolved = evolve_relational_drive(state, profile, now)
    kind = str(appraisal.kind)
    field = _KIND_FIELDS.get(kind)
    direction = str(appraisal.direction)
    if field is None or direction == DriveDirection.HOLD:
        return evolved
    magnitude = _appraisal_magnitude(str(appraisal.strength), profile.normalized().appraisal_sensitivity)
    if direction == DriveDirection.FALL:
        magnitude = -magnitude
    elif direction != DriveDirection.RISE:
        return evolved
    current = getattr(evolved, field)
    return evolved.with_values(**{field: _apply_delta(current, magnitude)}, updated_at=_aware(now))


def apply_drive_effect(
    state: RelationalDriveState,
    profile: RelationalDriveProfile,
    effect: DriveEffect,
    now: datetime,
) -> RelationalDriveState:
    evolved = evolve_relational_drive(state, profile, now)
    vector = _EFFECT_VECTORS.get(str(effect.event), {})
    if not vector:
        return evolved
    values = {
        "physical_arousal": evolved.physical_arousal,
        "erotic_salience": evolved.erotic_salience,
        "attachment_longing": evolved.attachment_longing,
        "afterglow": evolved.afterglow,
        "inhibition": evolved.inhibition,
    }
    scale = {
        DriveStrength.SUBTLE: 0.5,
        DriveStrength.MILD: 1.0,
        DriveStrength.STRONG: 1.5,
    }.get(str(effect.strength), 1.0)
    protect_longing = str(effect.event) in {
        DriveEffectEvent.HESITATION,
        DriveEffectEvent.STOPPED,
    }
    for field, delta in vector.items():
        if protect_longing and field == "attachment_longing":
            continue
        values[field] = _apply_delta(values[field], delta * scale)
    return evolved.with_values(**values, updated_at=_aware(now))


def drive_activation(state: RelationalDriveState) -> float:
    current = state.normalized()
    raw = (
        0.45 * current.physical_arousal
        + 0.35 * current.erotic_salience
        + 0.20 * current.attachment_longing * current.erotic_salience
        - current.inhibition
    )
    return _clamp(raw, _UNIT_MIN, _UNIT_MAX)


def _band_index(value: float, thresholds: tuple[float, float, float]) -> int:
    index = 0
    for threshold in thresholds:
        if value >= threshold:
            index += 1
        else:
            break
    return index


def drive_band(state: RelationalDriveState, previous_band: str | None = None) -> str:
    value = drive_activation(state)
    raw_index = _band_index(value, _BAND_THRESHOLDS)
    if previous_band not in _BANDS:
        return _BANDS[raw_index]
    previous = _BANDS.index(previous_band)
    up_index = _band_index(value, tuple(item + _BAND_MARGIN for item in _BAND_THRESHOLDS))
    down_index = _band_index(value, tuple(item - _BAND_MARGIN for item in _BAND_THRESHOLDS))
    if up_index > previous:
        return _BANDS[up_index]
    if down_index < previous:
        return _BANDS[down_index]
    return previous_band


_SUMMARIES = {
    "quiet": "身体上的冲动已经平缓，亲近的念头也只是淡淡存在着。这是一种内在倾向，不是行动指令。",
    "warm": "身体和心里都带着一点柔软的亲近。这是一种内在倾向，不是行动指令。",
    "drawn": "身体上的冲动已经平缓，但她仍在期待更亲密的独处。这是一种内在倾向，不是行动指令。",
    "strong": "亲近和身体上的期待都更鲜明了。这是一种内在倾向，不是行动指令。",
}


def build_drive_summary(state: RelationalDriveState, previous_band: str | None = None) -> str:
    return _SUMMARIES[drive_band(state, previous_band)]


def _exact_enum(value: Any, enum_cls: type[StrEnum]) -> str | None:
    text = str(value or "").strip()
    allowed = {item.value for item in enum_cls}
    if text not in allowed:
        return None
    return text


def parse_drive_appraisal(value: Any, *, allow_strong: bool = False) -> DriveAppraisal | None:
    if not isinstance(value, Mapping):
        return None
    if set(value) != {"kind", "direction", "strength"}:
        return None
    kind = _exact_enum(value.get("kind"), DriveKind)
    direction = _exact_enum(value.get("direction"), DriveDirection)
    strength = _exact_enum(value.get("strength"), DriveStrength)
    if kind is None or direction is None or strength is None:
        return None
    if strength == DriveStrength.STRONG and not allow_strong:
        return None
    return DriveAppraisal(kind=kind, direction=direction, strength=strength)


def parse_drive_effect(value: Any) -> DriveEffect | None:
    if not isinstance(value, Mapping):
        return None
    if set(value) != {"event", "strength"}:
        return None
    event = _exact_enum(value.get("event"), DriveEffectEvent)
    strength = _exact_enum(value.get("strength"), DriveStrength)
    if event is None or strength is None:
        return None
    return DriveEffect(event=event, strength=strength)
