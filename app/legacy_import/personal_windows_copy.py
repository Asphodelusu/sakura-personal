"""Hold native Windows read-sharing leases while copying an offline source.

This prevents writes/deletes to existing files. It is not a volume snapshot or
a process-stop detector: the caller must still ensure application quiescence,
and directory additions are detected by the copy inventory checks.
"""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
from pathlib import Path

from .personal_copy import PersonalCopyError, _inventory, _plain_path, prepare_personal_copy


class _SourceLease:
    def __init__(self, kernel):
        self._kernel = kernel
        self._handles = []
        self._held = False

    def is_held(self):
        return self._held

    def close(self):
        self._held = False
        error = False
        for handle in reversed(self._handles):
            if not self._kernel.CloseHandle(handle):
                error = True
        self._handles.clear()
        if error:
            raise PersonalCopyError("PERSONAL_COPY_HANDLE_RELEASE_FAILED")


def _kernel_api():
    if os.name != "nt":
        raise PersonalCopyError("PERSONAL_COPY_WINDOWS_REQUIRED")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


@contextmanager
def hold_windows_source_files(source, *, cancel_event=None):
    kernel = _kernel_api()
    root = Path(source).absolute()
    _plain_path(root)
    root = root.resolve(strict=True)
    # Remote shares have server-dependent locking/durability semantics.
    if str(root).startswith("\\\\") or not root.is_dir():
        raise PersonalCopyError("PERSONAL_COPY_LOCAL_DIRECTORY_REQUIRED")

    def check():
        if cancel_event is not None and cancel_event.is_set():
            raise PersonalCopyError("PERSONAL_COPY_CANCELLED")

    before = _inventory(root, check)
    lease = _SourceLease(kernel)
    invalid_handle = ctypes.c_void_p(-1).value
    try:
        # Hold directories first so their names cannot be replaced underneath
        # the remaining acquisitions. Source links were rejected by inventory.
        ordered = sorted(before, key=lambda path: (before[path][0] != "dir", len(path.parts), str(path)))
        for relative in ordered:
            check()
            path = root / relative
            _plain_path(path)
            flags = 0x02000000 if before[relative][0] == "dir" else 0x80
            handle = kernel.CreateFileW("\\\\?\\" + str(path), 0x80000000, 0x1,
                                        None, 3, flags, None)
            if handle == invalid_handle:
                code = ctypes.get_last_error()
                category = "SOURCE_BUSY" if code in (32, 33) else "SOURCE_OPEN_FAILED"
                raise PersonalCopyError("PERSONAL_COPY_" + category)
            lease._handles.append(handle)
        if _inventory(root, check) != before:
            raise PersonalCopyError("PERSONAL_COPY_SOURCE_CHANGED")
        lease._held = True
        yield lease
    finally:
        lease.close()


def prepare_windows_personal_copy(source, destination, *, source_is_quiescent, cancel_event=None):
    if source_is_quiescent() is not True:
        raise PersonalCopyError("PERSONAL_COPY_SOURCE_ACTIVE")
    with hold_windows_source_files(source, cancel_event=cancel_event) as lease:
        return prepare_personal_copy(source, destination,
            source_is_quiescent=lambda: lease.is_held() and source_is_quiescent() is True,
            cancel_event=cancel_event)
