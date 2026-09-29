"""Explicit offline personal-data trial copies; never a daily-runtime switch."""
from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat

from app.legacy_import.personal_copy import (
    PersonalCopyError, STATE_FILE, _plain_path, _same_bytes,
    prepare_personal_copy,
)
from app.legacy_import.personal_windows_copy import _kernel_api, _SourceLease

DOMAINS = ("data/memory", "data/chat_history", "data/runtime_state", "data/config", "characters")
STATE = "data/memory_curation_state.json"
OPTIONAL_DOMAINS = ("data/visual_observations", "data/intimacy_guide.txt",
                    "data/reminders.json", "data/screen_awareness_state.json")
VOICE_FILES = frozenset({
    "characters/Sakura/voice/models/Sakura-e15.ckpt",
    "characters/Sakura/voice/models/Sakura_e8_s7176.pth",
})


def _selected_inventory(root, check):
    entries = {}
    todo = [root / domain for domain in (*DOMAINS, STATE)]
    todo.extend(root / domain for domain in OPTIONAL_DOMAINS if os.path.lexists(root / domain))
    for path in (root, root / "data"):
        _plain_path(path)
        info = path.stat()
        entries[path.relative_to(root)] = ("dir", info.st_mtime_ns, info.st_ino)
    while todo:
        check()
        path = todo.pop()
        _plain_path(path)
        info = path.lstat()
        relative = path.relative_to(root)
        if stat.S_ISDIR(info.st_mode):
            entries[relative] = ("dir", info.st_mtime_ns, info.st_ino)
            todo.extend(path.iterdir())
        elif stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1 and relative.as_posix() not in VOICE_FILES:
                raise PersonalCopyError("PERSONAL_COPY_LINK_UNSUPPORTED")
            if path.name in {"personal_write_pending.json", STATE_FILE}:
                raise PersonalCopyError("PERSONAL_COPY_SOURCE_INCOMPLETE")
            entries[relative] = ("file", info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino)
        else:
            raise PersonalCopyError("PERSONAL_COPY_SPECIAL_FILE_UNSUPPORTED")
    return entries


@contextmanager
def _pin_selected(root, entries, check):
    kernel = _kernel_api()
    lease = _SourceLease(kernel)
    try:
        for relative in sorted(entries, key=lambda p: (entries[p][0] != "dir", len(p.parts), str(p))):
            check()
            path = root / relative
            _plain_path(path)
            flags = 0x02000000 if entries[relative][0] == "dir" else 0x80
            handle = kernel.CreateFileW("\\\\?\\" + str(path), 0x80000000, 0x1, None, 3, flags, None)
            if handle == ctypes.c_void_p(-1).value:
                code = "SOURCE_BUSY" if ctypes.get_last_error() in (32, 33) else "SOURCE_OPEN_FAILED"
                raise PersonalCopyError("PERSONAL_COPY_" + code)
            lease._handles.append(handle)
        yield
    finally:
        lease.close()


def _mark(root, value):
    temporary = root / (STATE_FILE + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(root / STATE_FILE)


def copy_baseline(source, destination, *, source_is_quiescent):
    source, destination = Path(source).absolute(), Path(destination).absolute()
    _plain_path(source)
    _plain_path(destination)
    source, destination = source.resolve(strict=True), destination.resolve(strict=False)
    if (str(source).startswith("\\\\") or not destination.parent.is_dir()
            or os.path.lexists(destination) or destination.is_relative_to(source)
            or source.is_relative_to(destination)):
        raise PersonalCopyError("PERSONAL_COPY_PATH_INVALID")

    def check():
        if source_is_quiescent() is not True:
            raise PersonalCopyError("PERSONAL_COPY_SOURCE_ACTIVE")

    check()
    before = _selected_inventory(source, check)
    files = [p for p in before if before[p][0] == "file"]
    total = sum(before[p][1] for p in files)
    if shutil.disk_usage(destination.parent).free < total + 1024 * 1024:
        raise PersonalCopyError("PERSONAL_COPY_SPACE_INSUFFICIENT")
    with _pin_selected(source, before, check):
        if _selected_inventory(source, check) != before:
            raise PersonalCopyError("PERSONAL_COPY_SOURCE_CHANGED")
        destination.mkdir()
        _mark(destination, {"state": "incomplete", "role": "baseline"})
        for relative in sorted(before, key=lambda p: len(p.parts)):
            if before[relative][0] == "dir" and relative != Path("."):
                (destination / relative).mkdir()
        for relative in files:
            check()
            _plain_path(destination / relative)
            shutil.copyfile(source / relative, destination / relative)
        for relative in files:
            _same_bytes(source / relative, destination / relative, check)
        if _selected_inventory(source, check) != before:
            raise PersonalCopyError("PERSONAL_COPY_SOURCE_CHANGED")
        check()
        result = {"state": "complete", "role": "baseline", "files": len(files), "bytes": total}
        _mark(destination, result)
    return result


def prepare_work(baseline, destination):
    baseline, destination = Path(baseline), Path(destination)
    marker = json.loads((baseline / STATE_FILE).read_text())
    if marker.get("state") != "complete" or marker.get("role") != "baseline":
        raise PersonalCopyError("PERSONAL_BASELINE_REQUIRED")
    from app.legacy_import.personal_windows_copy import hold_windows_source_files
    with hold_windows_source_files(baseline) as lease:
        result = prepare_personal_copy(baseline, destination, source_is_quiescent=lease.is_held)
    _mark(destination, {"state": "incomplete", "role": "work"})
    count = 0
    for domain in ("data/memory", "data/chat_history"):
        for path in sorted((destination / domain).rglob("*")):
            if not path.is_file() or path.suffix not in {".db", ".sqlite", ".sqlite3"}:
                continue
            # Only this newly created work copy can be recovered/checkpointed.
            connection = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True)
            try:
                if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise PersonalCopyError("PERSONAL_SQLITE_INTEGRITY_FAILED")
                if connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                    raise PersonalCopyError("PERSONAL_SQLITE_BUSY")
            finally:
                connection.close()
            count += 1
    result.update(role="work", sqlite_checked=count)
    _mark(destination, result)
    return result
