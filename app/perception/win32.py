"""Lightweight Windows-native helpers via ctypes — zero dependency.

Gracefully degrades to no-ops on non-Windows platforms.
"""

from __future__ import annotations

import os
import sys

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("dwTime", wintypes.DWORD),
        ]

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def get_active_window_title() -> str:
        try:
            hwnd = _user32.GetForegroundWindow()
            if not hwnd:
                return ""
            length = _user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return ""
            buf = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, buf, length + 1)
            return buf.value or ""
        except Exception:
            return ""

    def get_foreground_hwnd() -> int:
        """Foreground window handle as int; 0 if none/unavailable."""
        try:
            return int(_user32.GetForegroundWindow() or 0)
        except Exception:
            return 0

    def get_active_window_process_name() -> str:
        """Returns basename of foreground process exe, e.g. 'notepad.exe'. Empty on failure."""
        try:
            hwnd = _user32.GetForegroundWindow()
            if not hwnd:
                return ""
            pid = wintypes.DWORD(0)
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return ""
            hproc = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if not hproc:
                return ""
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(len(buf))
                ok = _kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size))
                if not ok:
                    return ""
                return os.path.basename(buf.value or "")
            finally:
                _kernel32.CloseHandle(hproc)
        except Exception:
            return ""

    def get_idle_seconds() -> float:
        try:
            lii = _LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
            if not _user32.GetLastInputInfo(ctypes.byref(lii)):
                return 0.0
            tick = _kernel32.GetTickCount()
            return max(0.0, ((tick - lii.dwTime) & 0xFFFFFFFF) / 1000.0)
        except Exception:
            return 0.0

else:

    def get_active_window_title() -> str:
        return ""

    def get_foreground_hwnd() -> int:
        return 0

    def get_active_window_process_name() -> str:
        return ""

    def get_idle_seconds() -> float:
        return 0.0
