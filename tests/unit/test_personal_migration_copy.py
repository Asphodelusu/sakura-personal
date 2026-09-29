import json
import threading

import pytest

from app.legacy_import import personal_copy as copying


def fixture_source(tmp_path):
    source = tmp_path / "baseline"
    (source / "data/memory").mkdir(parents=True)
    (source / "empty").mkdir()
    (source / "data/memory/fixture.db").write_bytes(b"synthetic database bytes")
    (source / "data/unknown.json").write_text('{"private":"fixture"}', encoding="utf-8")
    return source


def test_copy_preserves_unknown_files_and_empty_directories_and_can_recover_to_new_root(tmp_path):
    source = fixture_source(tmp_path)
    target = tmp_path / "work"
    result = copying.prepare_personal_copy(source, target, source_is_quiescent=lambda: True)
    assert result == {"state": "complete", "files": 2, "bytes": 45}
    assert (target / "empty").is_dir()
    assert (target / "data/unknown.json").read_bytes() == (source / "data/unknown.json").read_bytes()
    (target / "data/memory/fixture.db").write_bytes(b"failed conversion")
    restored = tmp_path / "restored"
    copying.prepare_personal_copy(source, restored, source_is_quiescent=lambda: True)
    assert (restored / "data/memory/fixture.db").read_bytes() == b"synthetic database bytes"
    assert (target / "data/memory/fixture.db").read_bytes() == b"failed conversion"


@pytest.mark.parametrize("case", ["existing", "nested", "ancestor", "active"])
def test_rejects_unsafe_target_or_active_source_before_writing(tmp_path, case):
    source = fixture_source(tmp_path)
    target = tmp_path / "target"
    if case == "existing":
        target.mkdir()
    elif case == "nested":
        target = source / "nested"
    elif case == "ancestor":
        target = tmp_path
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    with pytest.raises(copying.PersonalCopyError):
        copying.prepare_personal_copy(source, target, source_is_quiescent=lambda: case != "active")
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before


def test_interrupted_copy_is_never_published_or_overwritten(tmp_path, monkeypatch):
    source = fixture_source(tmp_path)
    target = tmp_path / "work"
    def fail(*args):
        raise OSError("synthetic disk error")
    monkeypatch.setattr(copying.shutil, "copyfile", fail)
    with pytest.raises(OSError):
        copying.prepare_personal_copy(source, target, source_is_quiescent=lambda: True)
    assert json.loads((target / copying.STATE_FILE).read_text())["state"] == "incomplete"
    with pytest.raises(copying.PersonalCopyError):
        copying.prepare_personal_copy(source, target, source_is_quiescent=lambda: True)


@pytest.mark.parametrize("change", ["source", "target", "cancel", "writer"])
def test_detects_changes_or_cancel_before_completion(tmp_path, monkeypatch, change):
    source = fixture_source(tmp_path)
    target = tmp_path / "work"
    original = copying.shutil.copyfile
    cancel = threading.Event()
    quiet = [True]
    def changed(src, dst):
        original(src, dst)
        if change == "source":
            (source / "added.txt").write_text("changed")
        elif change == "target":
            dst.write_bytes(b"corrupt")
        elif change == "cancel":
            cancel.set()
        else:
            quiet[0] = False
    monkeypatch.setattr(copying.shutil, "copyfile", changed)
    with pytest.raises(copying.PersonalCopyError):
        copying.prepare_personal_copy(source, target, source_is_quiescent=lambda: quiet[0], cancel_event=cancel)
    assert json.loads((target / copying.STATE_FILE).read_text())["state"] == "incomplete"


def test_incomplete_baseline_and_unresolved_write_cannot_be_used_for_recovery(tmp_path):
    source = fixture_source(tmp_path)
    (source / "data/memory/personal_write_pending.json").write_text("{}")
    with pytest.raises(copying.PersonalCopyError):
        copying.prepare_personal_copy(source, tmp_path / "work", source_is_quiescent=lambda: True)
    assert not (tmp_path / "work").exists()


def test_incomplete_copy_is_rejected_by_memory_admission(tmp_path):
    from plugins.builtin.sakura_mem0.index_contract import require_supported_index_layout
    source = fixture_source(tmp_path)
    (source / copying.STATE_FILE).write_text('{"state":"incomplete"}')
    with pytest.raises(RuntimeError, match="COPY_INCOMPLETE"):
        require_supported_index_layout(source / "data/memory")


def test_hardlinked_source_is_rejected_without_creating_target(tmp_path):
    import os
    source = fixture_source(tmp_path)
    os.link(source / "data/unknown.json", source / "alias.json")
    with pytest.raises(copying.PersonalCopyError, match="LINK"):
        copying.prepare_personal_copy(source, tmp_path / "work", source_is_quiescent=lambda: True)
    assert not (tmp_path / "work").exists()
