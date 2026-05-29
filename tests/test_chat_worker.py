from __future__ import annotations

import os

import pytest


def test_event_worker_can_move_to_qthread_without_overriding_qobject_event() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")

    qtcore = pytest.importorskip("PySide6.QtCore")
    qtwidgets = pytest.importorskip("PySide6.QtWidgets")
    if not hasattr(qtcore, "QThread") or not hasattr(qtwidgets, "QApplication"):
        pytest.skip("当前测试环境只提供了 PySide6 stub。")

    QThread = qtcore.QThread
    QApplication = qtwidgets.QApplication
    QWidget = qtwidgets.QWidget

    from app.agent import AgentEvent
    from app.chat_worker import EventWorker

    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    thread = QThread(parent)
    worker = EventWorker(object(), AgentEvent(type="reminder_due", payload={}))  # type: ignore[arg-type]

    worker.moveToThread(thread)

    assert worker.thread() is thread
    thread.deleteLater()
    parent.deleteLater()
    app.processEvents()
