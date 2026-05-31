from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from app.bootstrap import build_app_context
from app.character_loader import CharacterConfigError
from app.pet_window import PetWindow


BASE_DIR = Path(__file__).resolve().parent


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Sakura Desktop Pet")
    app.setQuitOnLastWindowClosed(False)

    try:
        context = build_app_context(BASE_DIR)
    except CharacterConfigError as exc:
        print(f"[Character] 配置无效：{exc}")
        return 1

    pet_window = PetWindow(context)
    pet_window.show()

    return app.exec()

if __name__ == "__main__":
    raise SystemExit(main())
