"""Atomic per-character relational-drive store. Tests use tmp_path only."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.relational_drive import DriveAppraisal, DriveEffect, RelationalDriveProfile
from app.storage.paths import StoragePaths
from app.storage.relational_drive import RelationalDriveStore, _LEDGER_LIMIT

UTC = timezone.utc


def _store(tmp_path: Path, session_scope: str = "demo") -> RelationalDriveStore:
    path = StoragePaths(tmp_path).relational_drive_for("Demo")
    return RelationalDriveStore(
        path,
        RelationalDriveProfile.natural_default(),
        session_scope=session_scope,
    )


def test_relational_drive_path_is_per_character(tmp_path: Path) -> None:
    paths = StoragePaths(tmp_path)
    assert paths.relational_drive_for("Sakura") == (
        tmp_path / "data" / "runtime_state" / "Sakura-relational-drive.json"
    )


def test_v1_migration_preserves_state_and_seeds_affectionate_anchor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    old_meaningful = datetime(2026, 8, 20, tzinfo=UTC)
    migration_now = old_meaningful
    v1_physical = 0.42
    old_keys = ["turn-legacy:effect"]
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": old_meaningful.isoformat(),
                "last_meaningful_contact_at": old_meaningful.isoformat(),
                "physical_arousal": v1_physical,
                "erotic_salience": 0.22,
                "attachment_longing": 0.31,
                "afterglow": 0.08,
                "inhibition": 0.05,
                "settled_keys": old_keys,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    migrated = store.snapshot(migration_now)
    payload = json.loads(store.path.read_text(encoding="utf-8"))

    assert migrated.last_affectionate_contact_at == migration_now
    assert migrated.physical_arousal == pytest.approx(v1_physical)
    assert migrated.last_meaningful_contact_at == old_meaningful
    assert payload["settled_keys"] == old_keys
    assert payload["version"] == 2
    assert payload["last_affectionate_contact_at"] == migration_now.isoformat()


def test_note_contact_does_not_change_affectionate_anchor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = datetime(2026, 9, 1, tzinfo=UTC)
    later = start + timedelta(days=1)
    store.reset(start)
    store.note_contact("contact-only", later)
    contacted = store.snapshot(later)
    assert contacted.last_meaningful_contact_at == later
    assert contacted.last_affectionate_contact_at == start


def test_qualifying_effect_updates_affectionate_contact_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = datetime(2026, 9, 1, tzinfo=UTC)
    later = start + timedelta(days=2)
    effect = DriveEffect(event="mutual_affection", strength="mild")
    assert store.settle_effect("turn-aff", effect, later) is True
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["last_affectionate_contact_at"] == later.isoformat()


def test_replayed_effect_keeps_first_affectionate_timestamp(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = datetime(2026, 9, 1, tzinfo=UTC)
    first_touch = start + timedelta(hours=6)
    replay_at = start + timedelta(days=1)
    effect = DriveEffect(event="mutual_escalation", strength="mild")
    store.settle_effect("turn-once", effect, first_touch)
    store.settle_effect("turn-once", effect, replay_at)
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["last_affectionate_contact_at"] == first_touch.isoformat()


def test_missing_file_returns_profile_baseline(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 8, 30, tzinfo=UTC)
    snap = store.snapshot(now)
    profile = RelationalDriveProfile.natural_default()
    assert snap.physical_arousal == profile.physical_baseline
    assert snap.erotic_salience == profile.salience_baseline
    assert not store.path.exists()


def test_snapshot_does_not_write_without_settlement(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = datetime(2026, 8, 30, tzinfo=UTC)
    store.snapshot(start)
    later = store.snapshot(start + timedelta(hours=3))
    assert later.physical_arousal == RelationalDriveProfile.natural_default().physical_baseline
    assert not store.path.exists()


def test_successful_contact_satisfies_longing_and_preserves_other_dimensions(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    profile = RelationalDriveProfile.natural_default()
    start = datetime(2026, 8, 27, tzinfo=UTC)
    reunion = start + timedelta(hours=72)
    assert store.note_contact("contact-1", start) is True
    grown = store.snapshot(reunion)
    assert grown.attachment_longing > 0.50
    affectionate = grown.last_affectionate_contact_at
    physical = grown.physical_arousal
    salience = grown.erotic_salience
    afterglow = grown.afterglow
    inhibition = grown.inhibition
    assert store.note_contact("contact-2", reunion) is True
    contacted = store.snapshot(reunion)
    assert contacted.attachment_longing == pytest.approx(profile.longing_baseline)
    assert contacted.last_meaningful_contact_at == reunion
    assert contacted.last_affectionate_contact_at == affectionate
    assert contacted.physical_arousal == pytest.approx(physical)
    assert contacted.erotic_salience == pytest.approx(salience)
    assert contacted.afterglow == pytest.approx(afterglow)
    assert contacted.inhibition == pytest.approx(inhibition)
    before = store.path.read_text(encoding="utf-8")
    assert store.note_contact("contact-2", reunion + timedelta(hours=1)) is False
    assert store.path.read_text(encoding="utf-8") == before
    replayed = store.snapshot(reunion)
    assert replayed.attachment_longing == pytest.approx(profile.longing_baseline)
    assert replayed.last_meaningful_contact_at == reunion
    assert replayed.last_affectionate_contact_at == affectionate


def test_replayed_contact_interaction_does_not_advance_contact_time(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = datetime(2026, 8, 30, tzinfo=UTC)
    store.note_contact("same-turn", start)
    store.note_contact("same-turn", start + timedelta(hours=12))
    replayed = store.snapshot(start + timedelta(hours=12))

    assert replayed.last_meaningful_contact_at == start
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["settled_keys"] == ["demo:same-turn:contact"]


def test_contact_without_interaction_id_does_not_persist(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 8, 30, tzinfo=UTC)

    store.note_contact("   ", now)

    assert store.snapshot(now).last_meaningful_contact_at is None
    assert not store.path.exists()


def test_note_contact_reports_whether_ledger_mutated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 8, 30, tzinfo=UTC)
    assert store.note_contact("turn-a", now) is True
    assert store.note_contact("turn-a", now + timedelta(hours=1)) is False
    assert store.note_contact("   ", now) is False
    assert store.note_contact("", now) is False


def test_same_interaction_cannot_settle_twice(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 8, 30, tzinfo=UTC)
    effect = DriveEffect(event="mutual_escalation", strength="mild")
    first = store.settle_effect("turn-1", effect, now)
    second = store.settle_effect("turn-1", effect, now)
    assert first is True
    assert second is False
    snap = store.snapshot(now)
    once = RelationalDriveStore(
        tmp_path / "other.json", RelationalDriveProfile.natural_default()
    )
    once.settle_effect("other", effect, now)
    assert snap.physical_arousal == once.snapshot(now).physical_arousal


def test_appraisal_and_effect_settle_independently_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 8, 30, tzinfo=UTC)
    appraisal = DriveAppraisal(kind="erotic_salience", direction="rise", strength="mild")
    effect = DriveEffect(event="mutual_affection", strength="mild")
    assert store.settle_appraisal("turn-9", appraisal, now) is True
    assert store.settle_appraisal("turn-9", appraisal, now) is False
    assert store.settle_effect("turn-9", effect, now) is True
    assert store.settle_effect("turn-9", effect, now) is False


def test_ledger_keeps_only_latest_128_keys(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 8, 30, tzinfo=UTC)
    effect = DriveEffect(event="aftercare", strength="subtle")
    for index in range(130):
        store.settle_effect(f"id-{index}", effect, now + timedelta(seconds=index))
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert len(payload["settled_keys"]) == 128
    assert "demo:id-0:effect" not in payload["settled_keys"]
    assert "demo:id-129:effect" in payload["settled_keys"]


def test_persisted_json_has_no_dialogue_or_event_bodies(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 8, 30, tzinfo=UTC)
    store.settle_effect("turn-z", DriveEffect(event="fulfilled", strength="mild"), now)
    raw = store.path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert set(payload) <= {
        "version",
        "updated_at",
        "last_meaningful_contact_at",
        "last_affectionate_contact_at",
        "physical_arousal",
        "erotic_salience",
        "attachment_longing",
        "afterglow",
        "inhibition",
        "settled_keys",
    }
    blob = raw.lower()
    for banned in ("fulfilled", "reason", "dialogue", "summary", "comment"):
        assert banned not in blob
    assert payload["version"] == 2
    assert isinstance(payload["physical_arousal"], float)


def test_corrupt_json_is_moved_aside_and_baseline_restored(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not-json", encoding="utf-8")
    now = datetime(2026, 8, 30, tzinfo=UTC)
    snap = store.snapshot(now)
    assert snap.physical_arousal == RelationalDriveProfile.natural_default().physical_baseline
    siblings = list(store.path.parent.glob("*.corrupt-*.json"))
    assert siblings
    assert siblings[0].read_text(encoding="utf-8") == "{not-json"
    assert store.path.is_file()
    restored = json.loads(store.path.read_text(encoding="utf-8"))
    assert restored["version"] == 2
    assert restored["last_affectionate_contact_at"] is not None
    assert restored["last_meaningful_contact_at"] is None


def test_invalid_schema_version_is_quarantined_and_baseline_restored(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps(
            {
                "version": "broken",
                "updated_at": "2026-09-01T00:00:00+00:00",
                "physical_arousal": 0.95,
            }
        ),
        encoding="utf-8",
    )
    now = datetime(2026, 9, 1, tzinfo=UTC)

    restored = store.snapshot(now)

    assert restored.physical_arousal == RelationalDriveProfile.natural_default().physical_baseline
    quarantined = list(store.path.parent.glob("*.corrupt-*.json"))
    assert len(quarantined) == 1
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["version"] == 2
    assert payload["last_affectionate_contact_at"] == now.isoformat()


def test_concurrent_distinct_ids_preserve_both_changes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 8, 30, tzinfo=UTC)
    errors: list[BaseException] = []

    def _one(key: str) -> None:
        try:
            store.settle_effect(key, DriveEffect(event="mutual_affection", strength="mild"), now)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    workers = [
        threading.Thread(target=_one, args=(f"thread-{index}",))
        for index in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert errors == []
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert set(payload["settled_keys"]) == {"demo:thread-0:effect", "demo:thread-1:effect"}


def test_successful_replace_leaves_no_tmp_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 8, 30, tzinfo=UTC)
    store.note_contact("ok", now)
    leftovers = list(store.path.parent.glob("*.tmp"))
    assert leftovers == []
    assert store.path.is_file()


def test_reset_returns_baseline_and_clears_ledger(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 8, 30, tzinfo=UTC)
    store.settle_effect("turn-x", DriveEffect(event="fulfilled", strength="mild"), now)
    reset = store.reset(now + timedelta(minutes=1))
    profile = RelationalDriveProfile.natural_default()
    assert reset.physical_arousal == profile.physical_baseline
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["settled_keys"] == []


def _scoped_store(tmp_path: Path, session_scope: str) -> RelationalDriveStore:
    path = StoragePaths(tmp_path).relational_drive_for("Demo")
    return RelationalDriveStore(
        path,
        RelationalDriveProfile.natural_default(),
        session_scope=session_scope,
    )


def test_distinct_session_scopes_allow_same_interaction_contact(tmp_path: Path) -> None:
    first_at = datetime(2026, 8, 31, 21, tzinfo=UTC)
    second_at = datetime(2026, 9, 2, 12, tzinfo=UTC)
    store_a = _scoped_store(tmp_path, "session-a")
    assert store_a.note_contact("interaction-1", first_at) is True
    store_b = _scoped_store(tmp_path, "session-b")
    assert store_b.note_contact("interaction-1", second_at) is True
    assert store_b.snapshot(second_at).last_meaningful_contact_at == second_at
    payload = json.loads(store_b.path.read_text(encoding="utf-8"))
    assert "session-a:interaction-1:contact" in payload["settled_keys"]
    assert "session-b:interaction-1:contact" in payload["settled_keys"]
    assert len(payload["settled_keys"]) == 2


def test_same_session_scope_keeps_appraisal_and_effect_exact_once(tmp_path: Path) -> None:
    store = _scoped_store(tmp_path, "session-a")
    now = datetime(2026, 8, 30, tzinfo=UTC)
    appraisal = DriveAppraisal(kind="erotic_salience", direction="rise", strength="mild")
    effect = DriveEffect(event="mutual_affection", strength="mild")
    assert store.settle_appraisal("interaction-1", appraisal, now) is True
    assert store.settle_appraisal("interaction-1", appraisal, now) is False
    assert store.settle_effect("interaction-1", effect, now) is True
    assert store.settle_effect("interaction-1", effect, now) is False


def test_scoped_ledger_trims_to_limit_and_drops_oldest(tmp_path: Path) -> None:
    store = _scoped_store(tmp_path, "session-a")
    now = datetime(2026, 8, 30, tzinfo=UTC)
    effect = DriveEffect(event="aftercare", strength="subtle")
    for index in range(200):
        store.settle_effect(f"id-{index}", effect, now + timedelta(seconds=index))
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    keys = payload["settled_keys"]
    assert len(keys) <= _LEDGER_LIMIT
    assert len(keys) == _LEDGER_LIMIT
    assert "session-a:id-0:effect" not in keys
    assert "session-a:id-199:effect" in keys


def test_legacy_unscoped_keys_do_not_block_new_scoped_contact(tmp_path: Path) -> None:
    store = _scoped_store(tmp_path, "session-new")
    now = datetime(2026, 9, 2, tzinfo=UTC)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps(
            {
                "version": 2,
                "updated_at": now.isoformat(),
                "last_meaningful_contact_at": now.isoformat(),
                "last_affectionate_contact_at": now.isoformat(),
                "physical_arousal": 0.10,
                "erotic_salience": 0.12,
                "attachment_longing": 0.05,
                "afterglow": 0.0,
                "inhibition": 0.0,
                "settled_keys": ["interaction-1:contact"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    later = now + timedelta(hours=1)
    assert store.note_contact("interaction-1", later) is True
    assert store.snapshot(later).last_meaningful_contact_at == later
    for index in range(_LEDGER_LIMIT - 1):
        assert store.note_contact(f"evict-{index}", later) is True
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    keys = payload["settled_keys"]
    assert "interaction-1:contact" not in keys
    assert "session-new:interaction-1:contact" in keys
    assert len(keys) == _LEDGER_LIMIT
