"""Readiness ownership when Core shutdown races plugin startup. Synthetic objects only."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.storage.runtime_roots import RuntimeRoots

WAIT = 3.0


class _GatedApplication:
    """A plugin owner whose close cancels a blocked start, like a stopped process."""

    def __init__(
        self,
        log: list[str],
        *,
        start_raises_after_cancel: bool = False,
        close_error: BaseException | None = None,
        bind_gate: threading.Event | None = None,
    ) -> None:
        self.log = log
        self.start_raises_after_cancel = start_raises_after_cancel
        self.close_error = close_error
        self.bind_gate = bind_gate
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.bind_entered = threading.Event()

    def bind_chat_boundary(self, _boundary: object) -> None:
        if self.bind_gate is not None:
            self.bind_entered.set()
            assert self.bind_gate.wait(WAIT)

    def start(self) -> None:
        self.log.append("start")
        self.started.set()
        woke = self.cancelled.wait(WAIT)
        self.log.append("start_returned" if woke else "start_timed_out")
        if self.start_raises_after_cancel:
            raise RuntimeError("plugin start cancelled")

    def bind_session(self, _session: object) -> None:
        self.log.append("bind_session")

    def close(self) -> None:
        self.log.append("close")
        self.cancelled.set()
        if self.close_error is not None:
            raise self.close_error


class _MCP:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def close(self) -> None:
        self.log.append("mcp_close")


def _controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    application: _GatedApplication,
    *,
    initializer_factory=None,
    mcp: _MCP | None = None,
):
    from app.core_host.server import HostConfig, ReadinessController

    monkeypatch.setattr(
        "app.config.web_plugin_migration.prepare_bundled_web_plugin",
        lambda _roots: None,
    )
    monkeypatch.setattr(
        "app.core_host.plugin_application.PluginApplicationHost",
        lambda *_args, **_kwargs: application,
    )
    if mcp is not None:
        monkeypatch.setattr(
            "app.agent.mcp.provider.start_mcp_tools_from_config",
            lambda *_args, **_kwargs: mcp,
        )
    if initializer_factory is None:
        def initializer_factory(*_args):
            raise AssertionError("shutdown must not initialize the Assistant")
    controller = ReadinessController(
        HostConfig(
            RuntimeRoots(tmp_path / "distribution", tmp_path / "user"),
            "00000000-0000-4000-8000-00000000b002",
            "ab" * 16,
        ),
        initializer_factory=initializer_factory,
    )
    if mcp is not None:
        controller.enable_mcp()
    controller.enable_plugins()
    return controller


def _release_and_join(controller, *gates: threading.Event) -> None:
    for gate in gates:
        gate.set()
    worker = controller._worker
    if worker is not None:
        worker.join(WAIT)
        assert not worker.is_alive()


@pytest.mark.parametrize("start_raises_after_cancel", [False, True])
def test_shutdown_during_plugin_start_closes_inflight_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, start_raises_after_cancel: bool,
) -> None:
    log: list[str] = []
    application = _GatedApplication(log, start_raises_after_cancel=start_raises_after_cancel)
    controller = _controller(tmp_path, monkeypatch, application)
    try:
        controller.begin({})
        assert application.started.wait(2)
        assert controller.published_plugin_application() is None

        controller.close()

        assert not controller._worker.is_alive()
        assert log == ["start", "close", "start_returned"]
        assert controller.published_plugin_application() is None
        assert controller.published_session() is None
        controller.close()
        assert log.count("close") == 1
    finally:
        _release_and_join(controller, application.cancelled)


def test_shutdown_during_plugin_start_keeps_unpublished_mcp_with_initializer_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[str] = []
    application = _GatedApplication(log)
    controller = _controller(tmp_path, monkeypatch, application, mcp=_MCP(log))
    try:
        controller.begin({})
        assert application.started.wait(2)

        controller.close()

        assert not controller._worker.is_alive()
        assert log == ["start", "close", "start_returned", "mcp_close"]
        assert controller.published_mcp_provider() is None
    finally:
        _release_and_join(controller, application.cancelled)


def test_shutdown_after_construction_before_start_registration_never_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[str] = []
    gate = threading.Event()
    application = _GatedApplication(log, bind_gate=gate)
    controller = _controller(tmp_path, monkeypatch, application, mcp=_MCP(log))
    controller.bind_chat_boundary(object())
    errors: list[BaseException] = []

    def close() -> None:
        try:
            controller.close()
        except BaseException as error:  # noqa: BLE001 - asserted below
            errors.append(error)

    closer = threading.Thread(target=close, name="test-startup-shutdown-close")
    try:
        controller.begin({})
        assert application.bind_entered.wait(2)
        closer.start()
        deadline = time.monotonic() + 2
        while not controller._closed and time.monotonic() < deadline:
            time.sleep(0.01)
        assert controller._closed
        gate.set()
        closer.join(WAIT)
        assert not closer.is_alive()

        assert errors == []
        assert not controller._worker.is_alive()
        assert log == ["close", "mcp_close"]
        assert controller.published_plugin_application() is None
    finally:
        gate.set()
        if closer.is_alive():
            closer.join(WAIT)
        _release_and_join(controller, application.cancelled)


def test_plugin_close_failure_during_startup_is_raised_and_mcp_still_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[str] = []
    application = _GatedApplication(log, close_error=RuntimeError("plugin cleanup failed"))
    controller = _controller(tmp_path, monkeypatch, application, mcp=_MCP(log))
    try:
        controller.begin({})
        assert application.started.wait(2)

        with pytest.raises(RuntimeError, match="plugin cleanup failed"):
            controller.close()

        assert not controller._worker.is_alive()
        assert log == ["start", "close", "start_returned", "mcp_close"]
        controller.close()
        assert log.count("close") == 1
    finally:
        _release_and_join(controller, application.cancelled)


def test_shutdown_during_assistant_initialize_closes_published_resources_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[str] = []
    application = _GatedApplication(log)
    application.cancelled.set()
    entered = threading.Event()

    class Initializer:
        def initialize(self, cancel: threading.Event) -> object:
            entered.set()
            assert cancel.wait(WAIT)
            return SimpleNamespace(
                state="ready",
                code="READY",
                retryable=False,
                current_character_summary=None,
                current_character_presentation=None,
                session=object(),
            )

        def close(self) -> None:
            log.append("initializer_close")

    controller = _controller(
        tmp_path,
        monkeypatch,
        application,
        initializer_factory=lambda *_args: Initializer(),
        mcp=_MCP(log),
    )
    try:
        controller.begin({})
        assert entered.wait(2)
        assert controller.published_plugin_application() is application

        controller.close()

        assert not controller._worker.is_alive()
        assert controller.published_session() is None
        assert "bind_session" not in log
        assert log.count("close") == 1
        assert log.count("mcp_close") == 1
        assert log.count("initializer_close") == 1
        assert log.index("close") < log.index("mcp_close")
    finally:
        _release_and_join(controller, application.cancelled)
