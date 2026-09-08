from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.support.production_data_guard import (
    ProductionDataWriteBlocked,
    ProductionDataWriteGuard,
)


def test_guard_blocks_mutating_audit_events_before_io(tmp_path: Path) -> None:
    data_root = tmp_path / "repo" / "data"
    guard = ProductionDataWriteGuard(data_root)
    target = data_root / "memory" / "state.json"

    attempts = (
        ("open", (target, "w", os.O_WRONLY | os.O_CREAT)),
        ("open", (target, None, os.O_RDWR | os.O_CREAT)),
        ("sqlite3.connect", (str(data_root / "memory" / "index.db"),)),
        (
            "sqlite3.connect",
            (f"file:{(data_root / 'memory' / 'index.db').as_posix()}?mode=ro",),
        ),
        ("os.remove", (target, -1)),
        ("os.mkdir", (data_root / "new", 0o777, -1)),
        ("os.rename", (tmp_path / "source", target, -1, -1)),
        ("os.rename", (target, tmp_path / "destination", -1, -1)),
    )

    for event, args in attempts:
        with pytest.raises(ProductionDataWriteBlocked, match="production data"):
            guard.audit(event, args)


def test_guard_allows_reads_and_paths_outside_production_data(tmp_path: Path) -> None:
    data_root = tmp_path / "repo" / "data"
    guard = ProductionDataWriteGuard(data_root)

    guard.audit("open", (data_root / "config" / "api.yaml", "r", os.O_RDONLY))
    guard.audit("open", (tmp_path / "pytest" / "result.json", "w", os.O_WRONLY))
    guard.audit("sqlite3.connect", (":memory:",))
    guard.audit("os.remove", (tmp_path / "pytest" / "old.json", -1))


def test_guard_signal_escapes_business_exception_fallbacks() -> None:
    assert not issubclass(ProductionDataWriteBlocked, Exception)
