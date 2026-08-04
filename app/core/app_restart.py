"""桌宠「重启 Sakura」的进程级策略。

由 bat 启动时：只退出 RESTART_EXIT_CODE，交给启动脚本清屏后重跑，
避免 startDetached 与 bat 末尾 pause 抢同一控制台（日志夹着「按任意键」）。

直接 python/冻结包启动时：startDetached 拉起新进程后以 0 退出。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Callable

from app.ui.tray_menu import RESTART_EXIT_CODE

SAKURA_LAUNCHER_RESTART_ENV = "SAKURA_LAUNCHER_RESTART"


def launcher_handles_restart(environ: Mapping[str, str] | None = None) -> bool:
    env = environ if environ is not None else os.environ
    return str(env.get(SAKURA_LAUNCHER_RESTART_ENV, "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def resolve_restart_exit_code(
    *,
    environ: Mapping[str, str] | None = None,
    frozen: bool | None = None,
    executable: str | None = None,
    argv: Sequence[str] | None = None,
    base_dir: Path | None = None,
    start_detached: Callable[[str, list[str], str], tuple[bool, int]] | None = None,
) -> int:
    """处理 RESTART_EXIT_CODE：返回最终应交给操作系统的退出码。"""
    if launcher_handles_restart(environ):
        return RESTART_EXIT_CODE

    program = executable if executable is not None else sys.executable
    args = list(argv if argv is not None else sys.argv)
    is_frozen = bool(getattr(sys, "frozen", False) if frozen is None else frozen)
    root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[2]

    if is_frozen:
        arguments = args[1:]
    else:
        arguments = [str(root / "main.py"), *args[1:]]

    if start_detached is None:
        from PySide6.QtCore import QProcess

        def start_detached(program_name: str, program_args: list[str], cwd: str) -> tuple[bool, int]:
            return QProcess.startDetached(program_name, program_args, cwd)

    started, _pid = start_detached(program, arguments, str(root))
    return 0 if started else 1
