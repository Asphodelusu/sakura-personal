"""Offline copy primitive, not a live-installation backup or migration CLI.

The caller must hold the source quiescent throughout this call. Repeated checks
and inventories detect changes but cannot establish quiescence on their own.
Never opens source databases, follows links, replaces a destination or deletes
an interrupted copy. A complete copy still needs semantic migration validation.
"""
import json
import os
from pathlib import Path
import shutil
import stat
from plugins.builtin.sakura_mem0.index_contract import COPY_STATE_FILE

STATE_FILE = COPY_STATE_FILE


class PersonalCopyError(RuntimeError):
    pass


def _plain_path(path):
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise PersonalCopyError("PERSONAL_COPY_LINK_UNSUPPORTED")


def _inventory(root, check):
    entries = {}
    todo = [root]
    while todo:
        check()
        current = todo.pop()
        _plain_path(current)
        info = current.lstat()
        relative = current.relative_to(root)
        if stat.S_ISDIR(info.st_mode):
            entries[relative] = ("dir", info.st_mtime_ns, info.st_ino)
            todo.extend(current.iterdir())
        elif stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1:
                raise PersonalCopyError("PERSONAL_COPY_LINK_UNSUPPORTED")
            if current.name == "personal_write_pending.json":
                raise PersonalCopyError("PERSONAL_COPY_SOURCE_INCOMPLETE")
            entries[relative] = ("file", info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino)
        else:
            raise PersonalCopyError("PERSONAL_COPY_SPECIAL_FILE_UNSUPPORTED")
    return entries


def _same_bytes(left, right, check):
    with left.open("rb") as source, right.open("rb") as target:
        while True:
            check()
            data = source.read(1024 * 1024)
            if target.read(1024 * 1024) != data:
                raise PersonalCopyError("PERSONAL_COPY_CONTENT_MISMATCH")
            if not data:
                return


def prepare_personal_copy(source, destination, *, source_is_quiescent, cancel_event=None):
    """Create a new offline work copy under an existing, exclusively owned parent.

    source_is_quiescent is a caller-provided ownership/liveness check, not a
    substitute for stopping actual writers. No production caller exists yet.
    """
    source, destination = Path(source).absolute(), Path(destination).absolute()
    _plain_path(source)
    _plain_path(destination)
    source, destination = source.resolve(strict=True), destination.resolve(strict=False)
    if (not source.is_dir() or not destination.parent.is_dir() or os.path.lexists(destination)
            or destination.is_relative_to(source) or source.is_relative_to(destination)):
        raise PersonalCopyError("PERSONAL_COPY_PATH_INVALID")

    def check():
        if cancel_event is not None and cancel_event.is_set():
            raise PersonalCopyError("PERSONAL_COPY_CANCELLED")
        if source_is_quiescent() is not True:
            raise PersonalCopyError("PERSONAL_COPY_SOURCE_ACTIVE")

    check()
    before = _inventory(source, check)
    source_state = source / STATE_FILE
    if source_state.exists():
        try:
            state = json.loads(source_state.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("state") != "complete":
                raise ValueError("incomplete")
        except (OSError, ValueError) as exc:
            raise PersonalCopyError("PERSONAL_COPY_SOURCE_INCOMPLETE") from exc
    files = [path for path, data in before.items() if data[0] == "file" and path != Path(STATE_FILE)]
    total = sum(before[path][1] for path in files)
    if shutil.disk_usage(destination.parent).free < total + 1024 * 1024:
        raise PersonalCopyError("PERSONAL_COPY_SPACE_INSUFFICIENT")
    check()
    destination.mkdir(exist_ok=False)
    marker = destination / STATE_FILE
    with marker.open("x", encoding="utf-8") as output:
        json.dump({"state": "incomplete"}, output)
        output.flush()
        os.fsync(output.fileno())
    for path, data in sorted(before.items(), key=lambda item: len(item[0].parts)):
        check()
        if data[0] == "dir" and path != Path("."):
            (destination / path).mkdir(exist_ok=False)
    for path in files:
        check()
        _plain_path(source / path)
        _plain_path(destination / path)
        shutil.copyfile(source / path, destination / path)
        _same_bytes(source / path, destination / path, check)
    if _inventory(source, check) != before:
        raise PersonalCopyError("PERSONAL_COPY_SOURCE_CHANGED")
    # Recheck every destination file before publishing the completion marker.
    for path in files:
        _plain_path(destination / path)
        _same_bytes(source / path, destination / path, check)
    if _inventory(source, check) != before:
        raise PersonalCopyError("PERSONAL_COPY_SOURCE_CHANGED")
    result = {"state": "complete", "files": len(files), "bytes": total}
    temporary = destination / (STATE_FILE + ".tmp")
    with temporary.open("x", encoding="utf-8") as output:
        json.dump(result, output)
        output.flush()
        os.fsync(output.fileno())
    check()
    temporary.replace(marker)
    return result
