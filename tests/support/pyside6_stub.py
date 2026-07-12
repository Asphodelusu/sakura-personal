"""在无 PySide6 的环境里为单元测试提供最小 QtCore 桩模块。"""

from __future__ import annotations

import importlib.util
import sys
import types
from importlib.machinery import ModuleSpec

_STUB_MARKER = "_SAKURA_PYTEST_STUB"


def is_pyside6_stub_active() -> bool:
    module = sys.modules.get("PySide6")
    return module is not None and getattr(module, _STUB_MARKER, False)


def install_pyside6_stub_if_missing() -> None:
    if importlib.util.find_spec("PySide6") is not None:
        return

    class _FakeQObject:
        def __init__(self, parent: object | None = None) -> None:
            self._parent = parent

        def children(self) -> list[object]:
            return []

    class _FakeQThread:
        def __init__(self, parent: object | None = None) -> None:
            self._parent = parent

        @staticmethod
        def currentThread() -> _FakeQThread:
            return _FakeQThread()

        def isRunning(self) -> bool:
            return False

        def quit(self) -> None:
            return None

        def wait(self, *_args: object, **_kwargs: object) -> bool:
            return True

        def terminate(self) -> None:
            return None

    class _FakeQTimer:
        @staticmethod
        def singleShot(*_args: object, **_kwargs: object) -> None:
            return None

    qtcore = types.ModuleType("PySide6.QtCore")
    qtcore.__spec__ = ModuleSpec("PySide6.QtCore", loader=None)
    qtcore.QObject = _FakeQObject
    qtcore.QThread = _FakeQThread
    qtcore.QTimer = _FakeQTimer

    pyside6 = types.ModuleType("PySide6")
    pyside6.__spec__ = ModuleSpec("PySide6", loader=None)
    setattr(pyside6, _STUB_MARKER, True)
    pyside6.QtCore = qtcore

    sys.modules["PySide6"] = pyside6
    sys.modules["PySide6.QtCore"] = qtcore
