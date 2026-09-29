import json
import os
import pytest
import subprocess
from pathlib import Path

import plugins.builtin.sakura_mem0.personal_core_profile as profiles


def _dir(tmp_path: Path) -> Path:
    memory = tmp_path / "memory"
    memory.mkdir()
    return memory


def _write(memory: Path, payload) -> Path:
    path = memory / "core_profiles.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _events(monkeypatch):
    captured = []
    monkeypatch.setattr(
        profiles,
        "log_event",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )
    return captured


def _directory_link(link, target):
    if os.name == "nt":
        subprocess.check_call(["cmd", "/c", "mklink", "/J", str(link), str(target)])
    else:
        link.symlink_to(target, target_is_directory=True)


def test_legacy_content_is_preferred_over_sections(tmp_path):
    memory = _dir(tmp_path)
    _write(memory, {"alice": {"content": "stored profile", "sections": {"今の関係": "section only"}}})
    fragment = profiles.read_personal_core_profile(memory, "alice")
    assert fragment["id"] == "core_profile:alice"
    assert fragment["sensitivity"] == "private"
    assert fragment["content"] == "【常驻档案】\nstored profile"
    assert "section only" not in fragment["content"]
    assert "trust" not in fragment


def test_memory_field_is_used_when_content_is_absent(tmp_path):
    memory = _dir(tmp_path)
    _write(memory, {"alice": {"memory": "from memory field"}})
    fragment = profiles.read_personal_core_profile(memory, "alice")
    assert fragment["content"] == "【常驻档案】\nfrom memory field"


def test_legacy_opaque_id_is_projected_to_current_scope(tmp_path):
    memory = _dir(tmp_path)
    path = _write(memory, {"alice": {"id": "legacy-record-id", "content": "legacy profile"}})
    before = path.read_bytes()
    fragment = profiles.read_personal_core_profile(memory, "alice")
    assert fragment["id"] == "core_profile:alice"
    assert path.read_bytes() == before


def test_v2_sections_are_joined_only_when_stored_text_is_empty(tmp_path, monkeypatch):
    events = _events(monkeypatch)
    memory = _dir(tmp_path)
    _write(memory, {
        "alice": {
            "schema_version": 2,
            "sections": {"今の関係": "一起生活", "あなたについて知っていること": "喜欢茶"},
        }
    })
    fragment = profiles.read_personal_core_profile(memory, "alice")
    assert fragment["content"] == "【常驻档案】\n一起生活\n\n喜欢茶"
    assert any(item[1].get("event") == "memory.personal.core_profile_sections_fallback" for item in events)
    assert "一起生活" not in str(events)


def test_incomplete_v2_sections_produce_no_fragment(tmp_path):
    memory = _dir(tmp_path)
    _write(memory, {"alice": {"schema_version": 2, "sections": {"今の関係": "一起生活", "今の私": "  "}}})
    assert profiles.read_personal_core_profile(memory, "alice") is None


def test_unknown_schema_reads_stored_text_without_section_fallback(tmp_path, monkeypatch):
    events = _events(monkeypatch)
    memory = _dir(tmp_path)
    path = _write(memory, {
        "alice": {"schema_version": 3, "content": "kept text", "sections": {"extra": "hidden section"}},
        "bob": {"content": "other scope"},
    })
    before = path.read_bytes()
    fragment = profiles.read_personal_core_profile(memory, "alice")
    assert fragment["content"] == "【常驻档案】\nkept text"
    assert "hidden section" not in fragment["content"]
    assert path.read_bytes() == before
    assert any(
        item[1].get("event") == "memory.personal.core_profile_unknown_schema"
        and item[0][2].get("schema_version") == 3
        for item in events
    )
    assert "kept text" not in str(events)
    assert profiles.read_personal_core_profile(memory, "carol") is None


def test_unknown_schema_without_stored_text_does_not_leak_sections(tmp_path, monkeypatch):
    events = _events(monkeypatch)
    memory = _dir(tmp_path)
    _write(memory, {"alice": {"schema_version": 3, "sections": {"extra": "hidden section"}}})
    assert profiles.read_personal_core_profile(memory, "alice") is None
    assert "hidden section" not in str(events)


def test_scope_mismatch_does_not_leak(tmp_path):
    memory = _dir(tmp_path)
    _write(memory, {"bob": {"content": "bob private profile"}})
    assert profiles.read_personal_core_profile(memory, "alice") is None


def test_missing_and_empty_files_have_no_fragment_or_diagnostic(tmp_path, monkeypatch):
    events = _events(monkeypatch)
    memory = _dir(tmp_path)
    assert profiles.read_personal_core_profile(memory, "alice") is None
    (memory / "core_profiles.json").write_bytes(b" \n")
    assert profiles.read_personal_core_profile(memory, "alice") is None
    (memory / "core_profiles.json").write_text("{}", encoding="utf-8")
    assert profiles.read_personal_core_profile(memory, "alice") is None
    assert events == []


def test_corrupt_file_is_diagnostic_only_and_unchanged(tmp_path, monkeypatch):
    events = _events(monkeypatch)
    memory = _dir(tmp_path)
    path = memory / "core_profiles.json"
    raw = "不是合法档案".encode() + b"\xff"
    path.write_bytes(raw)
    assert profiles.read_personal_core_profile(memory, "alice") is None
    assert path.read_bytes() == raw
    assert events[0][0][2]["code"] == "CORE_PROFILE_ENCODING"
    assert "不是合法档案" not in str(events)
    path.write_text('{"alice": ', encoding="utf-8")
    events.clear()
    assert profiles.read_personal_core_profile(memory, "alice") is None
    assert events[0][0][2]["code"] == "CORE_PROFILE_JSON"
    path.write_text("[]", encoding="utf-8")
    events.clear()
    assert profiles.read_personal_core_profile(memory, "alice") is None
    assert events[0][0][2]["code"] == "CORE_PROFILE_STRUCTURE"


def test_content_budget_includes_label(tmp_path):
    memory = _dir(tmp_path)
    _write(memory, {"alice": {"content": "a" * 5000}})
    fragment = profiles.read_personal_core_profile(memory, "alice")
    assert fragment["content"].startswith("【常驻档案】\n")
    assert len(fragment["content"]) == 1200
    assert fragment["content"].endswith("…")
    assert fragment["budgetHint"] == 1200


def test_substituted_link_or_hardlink_is_not_followed(tmp_path, monkeypatch):
    events = _events(monkeypatch)
    memory = _dir(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"alice": {"content": "escaped profile"}}), encoding="utf-8")
    linked = memory / "core_profiles.json"
    os.link(outside, linked)
    assert profiles.read_personal_core_profile(memory, "alice") is None
    assert events[0][0][2]["code"] == "CORE_PROFILE_LINK"
    assert "escaped profile" not in str(events)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "core_profiles.json").write_text(
        json.dumps({"alice": {"content": "junction profile"}}), encoding="utf-8"
    )
    junction = tmp_path / "junction-memory"
    _directory_link(junction, elsewhere)
    events.clear()
    assert profiles.read_personal_core_profile(junction, "alice") is None
    assert events[0][0][2]["code"] == "CORE_PROFILE_LINK"
    assert "junction profile" not in str(events)


def test_linked_ancestor_is_not_followed(tmp_path, monkeypatch):
    events = _events(monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    memory = _dir(outside)
    _write(memory, {"alice": {"content": "outside ancestor profile"}})
    alias = tmp_path / "substituted-parent"
    _directory_link(alias, outside)
    assert profiles.read_personal_core_profile(alias / "memory", "alice") is None
    assert events[0][0][2]["code"] == "CORE_PROFILE_LINK"
    assert "outside ancestor profile" not in str(events)


def test_unknown_schema_write_does_not_touch_source_or_backup(tmp_path):
    from plugins.builtin.sakura_mem0.personal_core_profile import (
        CoreProfileStorageError,
        patch_personal_core_profile_sections,
    )

    memory = _dir(tmp_path)
    path = _write(memory, {"alice": {"schema_version": 3, "content": "kept text"}})
    backup = path.with_name(path.name + ".bak")
    backup.write_bytes(b"old-backup")
    before = path.read_bytes()
    (memory / ".personal-write-rehearsal.json").write_text(
        json.dumps({"purpose": "personal-memory-write-rehearsal", "root": str(memory.resolve())}),
        encoding="utf-8",
    )
    (memory / ".sakura-personal-copy.json").write_text(json.dumps({"state": "complete"}), encoding="utf-8")
    with pytest.raises(CoreProfileStorageError):
        patch_personal_core_profile_sections(memory, "alice", "", {"今の関係": "新章节"})
    assert path.read_bytes() == before
    assert backup.read_bytes() == b"old-backup"


@pytest.mark.parametrize("identity", [
    {"id": "core_profile:bob"},
    {"scope": "bob"},
    {"metadata": {"scope": "bob"}},
])
def test_conflicting_record_scope_is_rejected(tmp_path, monkeypatch, identity):
    events = _events(monkeypatch)
    memory = _dir(tmp_path)
    path = _write(memory, {"alice": {"content": "foreign record", **identity}})
    before = path.read_bytes()
    assert profiles.read_personal_core_profile(memory, "alice") is None
    assert path.read_bytes() == before
    assert events[0][0][2]["code"] == "CORE_PROFILE_SCOPE"
    assert "foreign record" not in str(events)
