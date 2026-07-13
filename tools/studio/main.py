"""SakuraCharacterStudio 入口（已废弃）。

自 dev2 / 0.9.9-personal 起，角色工坊已迁移至 Tauri（``tools/studio-tauri``）。
请使用::

    python -m tools.studio_tauri.main
    start_studio.bat

旧版 Qt 实现保留在 ``tools/studio/`` 仅供对照；``python -m tools.studio.main``
会自动转发到 Tauri 宿主。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    sys.stderr.write(
        "[DEPRECATED] tools.studio.main 已废弃，正在启动 Tauri 角色工作室…\n"
    )
    from tools.studio_tauri.main import main as tauri_main

    return tauri_main()


if __name__ == "__main__":
    raise SystemExit(main())
