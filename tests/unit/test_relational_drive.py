"""Pure relational-drive numeric model: decay, appraisal, effects, bands."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.core.relational_drive import (
    DriveAppraisal,
    DriveEffect,
    RelationalDriveProfile,
    RelationalDriveState,
    apply_drive_appraisal,
    apply_drive_effect,
    build_drive_summary,
    drive_activation,
    drive_band,
    evolve_relational_drive,
    parse_drive_appraisal,
    parse_drive_effect,
)

UTC = timezone.utc


def test_natural_profile_has_low_nonzero_baselines() -> None:
    profile = RelationalDriveProfile.natural_default()
    assert profile.physical_baseline == 0.10
    assert profile.salience_baseline == 0.12
    assert profile.longing_baseline == 0.05
    assert profile.afterglow_baseline == 0.0


def test_all_state_dimensions_are_clamped_to_unit_interval() -> None:
    state = RelationalDriveState(
        physical_arousal=2.0,
        erotic_salience=-1.0,
        attachment_longing=3.0,
        afterglow=-2.0,
        inhibition=4.0,
        updated_at=datetime(2026, 8, 30, tzinfo=UTC),
    ).normalized()
    assert state.physical_arousal == 1.0
    assert state.erotic_salience == 0.0
    assert state.inhibition == 1.0


def test_profile_rejects_nan_and_clamps_half_lives() -> None:
    profile = RelationalDriveProfile(
        physical_baseline=float("nan"),
        salience_baseline=float("inf"),
        physical_half_life_hours=0.01,
        salience_half_life_hours=10_000.0,
        appraisal_sensitivity=-3.0,
    ).normalized()
    natural = RelationalDriveProfile.natural_default()
    assert profile.physical_baseline == natural.physical_baseline
    assert profile.salience_baseline == natural.salience_baseline
    assert profile.physical_half_life_hours == 0.25
    assert profile.salience_half_life_hours == 720.0
    assert profile.appraisal_sensitivity == 0.0


def test_physical_excess_halves_after_three_hours() -> None:
    profile = RelationalDriveProfile.natural_default()
    start = datetime(2026, 8, 30, 0, tzinfo=UTC)
    state = RelationalDriveState.from_profile(profile, now=start).with_values(
        physical_arousal=0.90
    )
    evolved = evolve_relational_drive(state, profile, start + timedelta(hours=3))
    assert evolved.physical_arousal == pytest.approx(0.50, abs=1e-6)


def test_clock_rollback_never_reverses_decay() -> None:
    profile = RelationalDriveProfile.natural_default()
    start = datetime(2026, 8, 30, 8, tzinfo=UTC)
    state = RelationalDriveState.from_profile(profile, now=start)
    assert evolve_relational_drive(state, profile, start - timedelta(hours=1)) == state


def test_longing_nears_cap_by_72_hours_without_unbounded_growth() -> None:
    profile = RelationalDriveProfile.natural_default()
    start = datetime(2026, 8, 27, tzinfo=UTC)
    state = RelationalDriveState.from_profile(
        profile, now=start, last_meaningful_contact_at=start
    )
    at_72 = evolve_relational_drive(state, profile, start + timedelta(hours=72))
    at_720 = evolve_relational_drive(state, profile, start + timedelta(hours=720))
    assert at_72.attachment_longing > 0.50
    assert at_720.attachment_longing <= 1.0
    assert at_720.attachment_longing - at_72.attachment_longing < 0.20


def test_longing_saturation_hours_bounds_further_absence_growth() -> None:
    profile = replace(
        RelationalDriveProfile.natural_default(),
        longing_saturation_hours=24.0,
    )
    start = datetime(2026, 8, 29, tzinfo=UTC)
    state = RelationalDriveState.from_profile(
        profile,
        now=start,
        last_meaningful_contact_at=start,
    )

    at_saturation = evolve_relational_drive(state, profile, start + timedelta(hours=24))
    much_later = evolve_relational_drive(state, profile, start + timedelta(hours=240))

    assert much_later.attachment_longing == pytest.approx(
        at_saturation.attachment_longing,
        abs=1e-6,
    )


def _apply_delta(current: float, delta: float) -> float:
    if delta > 0:
        return current + delta * (1.0 - current)
    return max(0.0, current + delta)


def test_appraisal_magnitudes_follow_sensitivity_and_reject_strong() -> None:
    profile = RelationalDriveProfile.natural_default()
    start = datetime(2026, 8, 30, tzinfo=UTC)
    state = RelationalDriveState.from_profile(profile, now=start)
    subtle = DriveAppraisal(kind="erotic_salience", direction="rise", strength="subtle")
    mild = DriveAppraisal(kind="erotic_salience", direction="rise", strength="mild")
    after_subtle = apply_drive_appraisal(state, profile, subtle, start)
    after_mild = apply_drive_appraisal(state, profile, mild, start)
    expected_subtle = _apply_delta(state.erotic_salience, 0.025 * profile.appraisal_sensitivity)
    expected_mild = _apply_delta(state.erotic_salience, 0.060 * profile.appraisal_sensitivity)
    assert after_subtle.erotic_salience == pytest.approx(expected_subtle, abs=1e-6)
    assert after_mild.erotic_salience == pytest.approx(expected_mild, abs=1e-6)
    assert parse_drive_appraisal(
        {"kind": "erotic_salience", "direction": "rise", "strength": "strong"}
    ) is None
    assert parse_drive_appraisal(
        {"kind": "erotic_salience", "direction": "rise", "strength": "strong"},
        allow_strong=True,
    ) is not None


def test_effect_vectors_match_approved_semantics() -> None:
    profile = RelationalDriveProfile.natural_default()
    start = datetime(2026, 8, 30, tzinfo=UTC)
    state = RelationalDriveState.from_profile(profile, now=start).with_values(
        attachment_longing=0.40
    )

    affection = apply_drive_effect(
        state, profile, DriveEffect(event="mutual_affection", strength="mild"), start
    )
    assert affection.physical_arousal == pytest.approx(
        _apply_delta(state.physical_arousal, 0.03), abs=1e-6
    )
    assert affection.erotic_salience == pytest.approx(
        _apply_delta(state.erotic_salience, 0.04), abs=1e-6
    )
    assert affection.attachment_longing == pytest.approx(
        _apply_delta(state.attachment_longing, -0.04), abs=1e-6
    )
    assert affection.afterglow == pytest.approx(_apply_delta(state.afterglow, 0.02), abs=1e-6)

    escalation = apply_drive_effect(
        state, profile, DriveEffect(event="mutual_escalation", strength="mild"), start
    )
    assert escalation.physical_arousal == pytest.approx(
        _apply_delta(state.physical_arousal, 0.12), abs=1e-6
    )
    assert escalation.erotic_salience == pytest.approx(
        _apply_delta(state.erotic_salience, 0.08), abs=1e-6
    )
    assert escalation.attachment_longing == pytest.approx(
        _apply_delta(state.attachment_longing, -0.05), abs=1e-6
    )

    fulfilled = apply_drive_effect(
        state, profile, DriveEffect(event="fulfilled", strength="mild"), start
    )
    assert fulfilled.physical_arousal == pytest.approx(
        _apply_delta(state.physical_arousal, -0.45), abs=1e-6
    )
    assert fulfilled.afterglow == pytest.approx(_apply_delta(state.afterglow, 0.55), abs=1e-6)

    aftercare = apply_drive_effect(
        state, profile, DriveEffect(event="aftercare", strength="mild"), start
    )
    assert aftercare.physical_arousal == pytest.approx(
        _apply_delta(state.physical_arousal, -0.10), abs=1e-6
    )
    assert aftercare.afterglow == pytest.approx(_apply_delta(state.afterglow, 0.12), abs=1e-6)

    hesitation = apply_drive_effect(
        state, profile, DriveEffect(event="hesitation", strength="mild"), start
    )
    stopped = apply_drive_effect(
        state, profile, DriveEffect(event="stopped", strength="mild"), start
    )
    assert hesitation.inhibition == pytest.approx(_apply_delta(state.inhibition, 0.15), abs=1e-6)
    assert stopped.inhibition == pytest.approx(_apply_delta(state.inhibition, 0.25), abs=1e-6)
    assert hesitation.attachment_longing >= state.attachment_longing
    assert stopped.attachment_longing >= state.attachment_longing


def test_high_values_saturate_instead_of_jumping() -> None:
    profile = RelationalDriveProfile.natural_default()
    start = datetime(2026, 8, 30, tzinfo=UTC)
    state = RelationalDriveState.from_profile(profile, now=start).with_values(
        physical_arousal=0.92
    )
    first = apply_drive_effect(
        state, profile, DriveEffect(event="mutual_escalation", strength="mild"), start
    )
    second = apply_drive_effect(
        first, profile, DriveEffect(event="mutual_escalation", strength="mild"), start
    )
    raw_jump = state.physical_arousal + 0.12 + 0.12
    assert first.physical_arousal < 1.0
    assert second.physical_arousal <= 1.0
    assert second.physical_arousal - first.physical_arousal < first.physical_arousal - state.physical_arousal
    assert second.physical_arousal < raw_jump


def test_parse_rejects_unknown_keys_and_malformed_values() -> None:
    assert parse_drive_appraisal(None) is None
    assert parse_drive_appraisal("bad") is None
    assert parse_drive_appraisal({"kind": "unknown", "direction": "rise", "strength": "subtle"}) is None
    assert parse_drive_appraisal(
        {"kind": "afterglow", "direction": "rise", "strength": "subtle", "reason": "x"}
    ) is None
    assert parse_drive_effect({"event": "unknown", "strength": "mild"}) is None
    assert parse_drive_effect({"event": "fulfilled", "strength": "mild", "text": "secret"}) is None
    parsed = parse_drive_effect({"event": "mutual_affection", "strength": "strong"})
    assert parsed is not None
    assert parsed.event == "mutual_affection"
    assert parsed.strength == "strong"


def test_summary_has_no_digits_or_imperatives() -> None:
    profile = RelationalDriveProfile.natural_default()
    start = datetime(2026, 8, 30, tzinfo=UTC)
    state = RelationalDriveState.from_profile(profile, now=start)
    summary = build_drive_summary(state)
    assert not any(ch.isdigit() for ch in summary)
    assert "必须" not in summary
    assert "应该" not in summary
    assert "数值" not in summary
    assert summary.strip()


def test_activation_bands_and_hysteresis() -> None:
    profile = RelationalDriveProfile.natural_default()
    start = datetime(2026, 8, 30, tzinfo=UTC)
    quiet = RelationalDriveState.from_profile(profile, now=start)
    assert drive_band(quiet) == "quiet"
    warm = quiet.with_values(physical_arousal=0.40, erotic_salience=0.40)
    assert drive_activation(warm) >= 0.25
    assert drive_band(warm) == "warm"
    edge = quiet.with_values(physical_arousal=0.40, erotic_salience=0.22, inhibition=0.0)
    assert 0.25 <= drive_activation(edge) < 0.30
    assert drive_band(edge) == "warm"
    assert drive_band(edge, previous_band="quiet") == "quiet"
    strong = quiet.with_values(
        physical_arousal=0.95,
        erotic_salience=0.90,
        attachment_longing=0.90,
        inhibition=0.0,
    )
    assert drive_band(strong) == "strong"
