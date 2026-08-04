"""托盘重启：bat 托管 vs startDetached。"""

from __future__ import annotations

from pathlib import Path

from app.core.app_restart import (
    SAKURA_LAUNCHER_RESTART_ENV,
    launcher_handles_restart,
    resolve_restart_exit_code,
)
from app.ui.tray_menu import RESTART_EXIT_CODE


def test_launcher_handles_restart_env() -> None:
    assert launcher_handles_restart({SAKURA_LAUNCHER_RESTART_ENV: "1"}) is True
    assert launcher_handles_restart({SAKURA_LAUNCHER_RESTART_ENV: "yes"}) is True
    assert launcher_handles_restart({}) is False


def test_resolve_restart_delegates_to_launcher() -> None:
    calls: list[object] = []

    def boom(*_a: object, **_k: object) -> tuple[bool, int]:
        calls.append(True)
        return True, 1

    code = resolve_restart_exit_code(
        environ={SAKURA_LAUNCHER_RESTART_ENV: "1"},
        start_detached=boom,
    )
    assert code == RESTART_EXIT_CODE
    assert calls == []


def test_resolve_restart_start_detached_without_launcher(tmp_path: Path) -> None:
    seen: list[tuple[str, list[str], str]] = []

    def fake_start(program: str, args: list[str], cwd: str) -> tuple[bool, int]:
        seen.append((program, args, cwd))
        return True, 42

    code = resolve_restart_exit_code(
        environ={},
        frozen=False,
        executable="python.exe",
        argv=["main.py", "--flag"],
        base_dir=tmp_path,
        start_detached=fake_start,
    )
    assert code == 0
    assert len(seen) == 1
    assert seen[0][0] == "python.exe"
    assert seen[0][1] == [str(tmp_path / "main.py"), "--flag"]
    assert seen[0][2] == str(tmp_path)
