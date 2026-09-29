import os
import json
import threading
from pathlib import Path

import pytest

from app.legacy_import.personal_copy import PersonalCopyError
from app.legacy_import import personal_windows_copy as windows_copy

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows file sharing contract")


def source_files(tmp_path):
    root = tmp_path / "source"
    (root / "data").mkdir(parents=True)
    (root / "data/a.db").write_bytes(b"synthetic-a")
    (root / "data/b.db").write_bytes(b"synthetic-b")
    return root


def test_existing_writer_prevents_copy_and_releases_already_acquired_handles(tmp_path):
    source = source_files(tmp_path)
    target = tmp_path / "copy"
    with (source / "data/b.db").open("r+b"):
        with pytest.raises(PersonalCopyError, match="SOURCE_BUSY"):
            windows_copy.prepare_windows_personal_copy(source, target, source_is_quiescent=lambda: True)
    assert not target.exists()
    # a.db was pinned before b.db failed; the exception must release it.
    (source / "data/a.db").write_bytes(b"still writable")


def test_lease_prevents_write_delete_and_replace_until_released(tmp_path):
    source = source_files(tmp_path)
    path = source / "data/a.db"
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"replacement")
    with windows_copy.hold_windows_source_files(source) as lease:
        assert lease.is_held()
        assert path.read_bytes() == b"synthetic-a"
        for change in (lambda: path.write_bytes(b"changed"), path.unlink, lambda: replacement.replace(path)):
            with pytest.raises(OSError):
                change()
    assert not lease.is_held()
    path.write_bytes(b"released")


def test_copy_holds_native_lease_for_entire_copy_and_releases_afterward(tmp_path, monkeypatch):
    from app.legacy_import import personal_copy
    source = source_files(tmp_path)
    original = personal_copy.shutil.copyfile
    writes_blocked = []
    def copying(src, dst):
        with pytest.raises(OSError):
            Path(src).write_bytes(b"competing write")
        writes_blocked.append(src)
        return original(src, dst)
    monkeypatch.setattr(personal_copy.shutil, "copyfile", copying)
    result = windows_copy.prepare_windows_personal_copy(source, tmp_path / "copy", source_is_quiescent=lambda: True)
    assert result["state"] == "complete" and len(writes_blocked) == 2
    assert (tmp_path / "copy/data/a.db").read_bytes() == b"synthetic-a"
    (source / "data/a.db").write_bytes(b"released")


def test_copy_error_releases_native_handles(tmp_path, monkeypatch):
    from app.legacy_import import personal_copy
    source = source_files(tmp_path)
    def fail(*args):
        raise OSError("synthetic copy error")
    monkeypatch.setattr(personal_copy.shutil, "copyfile", fail)
    with pytest.raises(OSError, match="synthetic copy error"):
        windows_copy.prepare_windows_personal_copy(source, tmp_path / "copy", source_is_quiescent=lambda: True)
    (source / "data/a.db").write_bytes(b"released")


def test_native_lease_does_not_replace_external_quiescence_check(tmp_path):
    source = source_files(tmp_path)
    with pytest.raises(PersonalCopyError, match="SOURCE_ACTIVE"):
        windows_copy.prepare_windows_personal_copy(source, tmp_path / "copy", source_is_quiescent=lambda: False)
    assert not (tmp_path / "copy").exists()


@pytest.mark.parametrize("change", ["cancel", "new_file"])
def test_interruption_or_directory_change_does_not_publish_copy_and_releases_lease(tmp_path, monkeypatch, change):
    from app.legacy_import import personal_copy
    source = source_files(tmp_path)
    original = personal_copy.shutil.copyfile
    cancel = threading.Event()
    def changed(src, dst):
        original(src, dst)
        if change == "cancel":
            cancel.set()
        else:
            (source / "new.txt").write_bytes(b"new file")
    monkeypatch.setattr(personal_copy.shutil, "copyfile", changed)
    with pytest.raises(PersonalCopyError):
        windows_copy.prepare_windows_personal_copy(source, tmp_path / "copy",
            source_is_quiescent=lambda: True, cancel_event=cancel)
    state = json.loads((tmp_path / "copy" / personal_copy.STATE_FILE).read_text())
    assert state["state"] == "incomplete"
    (source / "data/a.db").write_bytes(b"released")
