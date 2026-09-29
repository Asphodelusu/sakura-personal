"""Synthetic contracts for the explicit personal daily write entry."""
import json
import os
from pathlib import Path

import pytest

from plugins.builtin.sakura_mem0 import personal_records as records
from plugins.builtin.sakura_mem0.personal_core_profile import patch_personal_core_profile_sections
from plugins.builtin.sakura_mem0.personal_profile_maintenance import ProfileMaintenance

from test_personal_memory_backend import dependencies
from test_personal_model_loading import model_fixture
from test_personal_core_maintenance import _profile, UPDATED, SECTION


def _daily(root, scopes=("alice",)):
    marker = root / ".personal-daily.json"
    marker.write_text(json.dumps({"schemaVersion": 1, "purpose": "personal-memory-daily",
                                  "root": str(root), "scopes": list(scopes)}), encoding="utf-8")
    (root / ".sakura-personal-copy.json").write_text(json.dumps({"state": "complete"}), encoding="utf-8")
    return marker


@pytest.mark.parametrize("damage", ["missing", "root", "scope", "schema", "boolean_schema",
                                    "empty", "non_string", "duplicate", "copy", "linked_marker"])
def test_daily_rejects_invalid_admission_before_model_load(tmp_path, dependencies, monkeypatch, damage):
    root, snapshot, _, calls = model_fixture(tmp_path, dependencies, monkeypatch, 384)
    marker = _daily(root)
    body = json.loads(marker.read_text(encoding="utf-8"))
    if damage == "missing":
        marker.unlink()
    elif damage == "root":
        body["root"] = str(tmp_path / "other")
    elif damage == "scope":
        body["scopes"] = ["bob"]
    elif damage == "schema":
        body["schemaVersion"] = 2
    elif damage == "boolean_schema":
        body["schemaVersion"] = True
    elif damage == "empty":
        body["scopes"] = []
    elif damage == "non_string":
        body["scopes"] = ["alice", 123]
    elif damage == "duplicate":
        body["scopes"] = ["alice", "alice"]
    elif damage == "linked_marker":
        outside = tmp_path / "linked-daily.json"
        outside.write_bytes(marker.read_bytes())
        marker.unlink()
        os.link(outside, marker)
    else:
        (root / ".sakura-personal-copy.json").write_text(json.dumps({"state": "copying"}))
    if marker.exists() and damage != "linked_marker":
        marker.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(ValueError, match="PERSONAL_DAILY"):
        records.open_personal_memory_from_snapshot(root, snapshot=snapshot, daily=True, scope="alice")
    assert calls == []


def test_daily_writes_reopens_and_revocation_blocks_next_operation(tmp_path, dependencies, monkeypatch):
    root, snapshot, _, _ = model_fixture(tmp_path, dependencies, monkeypatch, 384)
    marker = _daily(root)
    store = records.open_personal_memory_from_snapshot(root, snapshot=snapshot, daily=True, scope="alice")
    try:
        item = store.create("alice", "synthetic daily fact")
        assert item["memory"] == "synthetic daily fact"
        with pytest.raises(ValueError, match="PERSONAL_DAILY"):
            store.create("bob", "foreign fact")
        marker.unlink()
        with pytest.raises(ValueError, match="PERSONAL_DAILY"):
            store.create("alice", "after revocation")
    finally:
        store.close()
    _daily(root)
    reopened = records.open_personal_memory_from_snapshot(root, snapshot=snapshot, daily=True, scope="alice")
    try:
        assert reopened.get("alice", item["id"])["memory"] == "synthetic daily fact"
    finally:
        reopened.close()


def test_daily_profile_and_maintenance_do_not_require_rehearsal_marker(tmp_path):
    root = tmp_path / "memory"
    root.mkdir()
    marker = _daily(root)
    _profile(root)
    assert not (root / records.WRITE_REHEARSAL).exists()
    assert patch_personal_core_profile_sections(root, "alice", UPDATED, {SECTION: "synthetic"}, daily=True)
    maintenance = ProfileMaintenance(root, "alice", settings={"enabled": True}, daily=True)
    try:
        maintenance.persist_candidates([], [])
    finally:
        maintenance.close()
    marker.unlink()
    with pytest.raises(ValueError, match="PERSONAL_DAILY"):
        patch_personal_core_profile_sections(root, "alice", UPDATED, {SECTION: "blocked"}, daily=True)


def test_daily_and_rehearsal_modes_cannot_coexist(tmp_path):
    with pytest.raises(ValueError, match="CONFLICT"):
        records.open_personal_memory_from_snapshot(tmp_path, snapshot=tmp_path,
                                                   daily=True, scope="alice", write_rehearsal=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows extended path admission")
@pytest.mark.parametrize("extended_marker", [False, True])
def test_daily_accepts_equivalent_windows_extended_paths(tmp_path, extended_marker):
    root = tmp_path / "memory"
    root.mkdir()
    marker = _daily(root)
    body = json.loads(marker.read_text(encoding="utf-8"))
    extended = Path("\\\\?\\" + str(root))
    if extended_marker:
        body["root"] = str(extended).upper()
        marker.write_text(json.dumps(body), encoding="utf-8")
    records._require_daily(root if extended_marker else extended, "alice")
    with pytest.raises(ValueError, match="PERSONAL_DAILY"):
        records._require_daily(root, "bob")


def test_daily_entry_is_explicit_and_conflicting_flags_fail(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from plugins.builtin.sakura_mem0.plugin import PersonalDailyPlugin, SakuraMem0Plugin

    with pytest.raises(ValueError, match="CONFLICT"):
        SakuraMem0Plugin(personal_snapshot=tmp_path, personal_daily=True,
                        personal_write_rehearsal=True)
    captured = []
    monkeypatch.setattr(SakuraMem0Plugin, "setup", lambda self, context: captured.append(self))
    context = SimpleNamespace(config=SimpleNamespace(get=lambda: {"personalSnapshot": str(tmp_path)}))
    PersonalDailyPlugin().setup(context)
    assert captured[0]._personal_daily is True
    assert SakuraMem0Plugin(personal_snapshot=tmp_path)._personal_daily is False
