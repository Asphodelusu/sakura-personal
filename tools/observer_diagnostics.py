"""Print focus-observer diagnostics. This does not start Sakura or open a window.

Create ``logs/observer-diagnostics.enabled`` in the active user root, or set
``SAKURA_OBSERVER_DIAGNOSTICS=1``, then follow ``logs/observer-diagnostics.jsonl``.
Lines are UTF-8 JSON and never include window titles or screen text.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def new_cursor() -> dict[str, object]:
    return {"offset": 0, "dev": None, "ino": None, "pending": b""}


def read_new_lines(path: Path, cursor: dict[str, object] | int | None = None) -> tuple[list[str], dict[str, object]]:
    """Return only complete UTF-8 lines. Identity comes from the opened handle."""
    if isinstance(cursor, int):
        state = new_cursor()
        state["offset"] = cursor
    elif isinstance(cursor, dict):
        state = cursor
    else:
        state = new_cursor()
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return [], state
    with handle:
        try:
            info = os.fstat(handle.fileno())
        except OSError:
            return [], state
        identity = (info.st_dev, info.st_ino)
        offset = int(state.get("offset") or 0)
        pending = state.get("pending")
        if not isinstance(pending, (bytes, bytearray)):
            pending = b""
        if state.get("dev") != identity[0] or state.get("ino") != identity[1] or info.st_size < offset:
            offset = 0
            pending = b""
            state = new_cursor()
        state["dev"] = identity[0]
        state["ino"] = identity[1]
        handle.seek(offset)
        chunk = handle.read()
        state["offset"] = offset + len(chunk)
    blob = bytes(pending) + chunk
    if not blob:
        state["pending"] = b""
        return [], state
    if blob.endswith(b"\n"):
        complete, rest = blob, b""
    else:
        split_at = blob.rfind(b"\n")
        if split_at < 0:
            state["pending"] = blob
            return [], state
        complete, rest = blob[: split_at + 1], blob[split_at + 1 :]
    state["pending"] = rest
    text = complete.decode("utf-8", errors="replace")
    return [line for line in text.splitlines() if line], state


def format_observer_event(line: str) -> str:
    """Readable console line. Keeps the source kind and reason."""
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return line
    if not isinstance(record, dict):
        return line
    kind = str(record.get("kind") or "")
    reason = str(record.get("reason") or "")
    process = str(record.get("process") or "")
    trigger = str(record.get("trigger") or "")
    return f"{kind} reason={reason} process={process} trigger={trigger}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Follow Sakura focus-observer diagnostics.")
    parser.add_argument("--file", required=True, help="Path to observer-diagnostics.jsonl")
    parser.add_argument("--once", action="store_true", help="Print the current file and exit")
    parser.add_argument("--readable", action="store_true", help="Print kind and reason instead of raw JSON")
    args = parser.parse_args(argv)
    path = Path(args.file)
    cursor = new_cursor()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    while True:
        lines, cursor = read_new_lines(path, cursor)
        for line in lines:
            rendered = format_observer_event(line) if args.readable else line
            sys.stdout.write(rendered + "\n")
        sys.stdout.flush()
        if args.once:
            return 0
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
