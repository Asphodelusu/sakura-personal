"""SakuraCharacterStudio 兼容入口（转发至 Tauri）。

请优先使用::

    python -m tools.studio_tauri.main
    start_studio.bat
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    from tools.studio_tauri.main import main as tauri_main

    return tauri_main()


if __name__ == "__main__":
    raise SystemExit(main())
