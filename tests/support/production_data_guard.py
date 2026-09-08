from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit


class ProductionDataWriteBlocked(BaseException):
    """Raised before a test can mutate the checkout's production data tree."""


class ProductionDataWriteGuard:
    def __init__(self, data_root: Path) -> None:
        self.data_root = Path(data_root).resolve()

    def audit(self, event: str, args: tuple[Any, ...]) -> None:
        paths: tuple[Any, ...] = ()
        if event == "open":
            if not _open_is_mutating(args):
                return
            paths = args[:1]
        elif event == "sqlite3.connect":
            database = args[0] if args else None
            if _is_memory_database(database):
                return
            paths = args[:1]
        elif event in {
            "os.remove",
            "os.rmdir",
            "os.mkdir",
            "os.truncate",
            "os.chmod",
            "os.chown",
            "os.utime",
            "shutil.rmtree",
            "shutil.chown",
        }:
            paths = args[:1]
        elif event in {
            "os.rename",
            "os.link",
            "os.symlink",
            "shutil.copyfile",
            "shutil.copymode",
            "shutil.copystat",
            "shutil.copytree",
            "shutil.move",
        }:
            paths = args[:2]
        else:
            return

        for raw_path in paths:
            resolved = _resolve_path(raw_path)
            if resolved is not None and _is_within(resolved, self.data_root):
                raise ProductionDataWriteBlocked(
                    f"test attempted production data mutation via {event}: {resolved}"
                )


def _open_is_mutating(args: tuple[Any, ...]) -> bool:
    mode = args[1] if len(args) > 1 else None
    flags = args[2] if len(args) > 2 else 0
    if isinstance(mode, str) and any(marker in mode for marker in "wax+"):
        return True
    if not isinstance(flags, int):
        return False
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    return bool(flags & write_flags)


def _is_memory_database(value: Any) -> bool:
    if value == ":memory:":
        return True
    if isinstance(value, str):
        return value.startswith("file::memory:")
    return False


def _resolve_path(value: Any) -> Path | None:
    if isinstance(value, int) or value is None:
        return None
    try:
        text = os.fsdecode(value)
        if text.startswith("file:"):
            parsed = urlsplit(text)
            text = unquote(parsed.path)
            if parsed.netloc:
                text = f"//{parsed.netloc}{text}"
            elif os.name == "nt" and len(text) >= 3 and text[0] == "/" and text[2] == ":":
                text = text[1:]
        return Path(text).resolve()
    except (OSError, TypeError, ValueError):
        return None


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents
