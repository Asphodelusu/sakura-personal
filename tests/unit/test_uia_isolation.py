# -*- coding: utf-8 -*-
"""Regression tests for the Observer UIA isolation boundary."""

from __future__ import annotations

import threading
import time

import pytest

from app.perception import observer
from app.perception.screen_reader import WindowText


def _wait_until(predicate, timeout: float = 0.5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.001)
    raise AssertionError("condition was not reached before timeout")


def _uia_worker_is_running() -> bool:
    with observer._uia_worker_lock:
        return bool(
            observer._uia_worker_thread
            and observer._uia_worker_thread.is_alive()
        )


@pytest.fixture(autouse=True)
def _reset_uia_isolation_state() -> None:
    with observer._uia_worker_lock:
        observer._uia_worker_thread = None
        observer._uia_consecutive_timeouts = 0
        observer._uia_circuit_open_until = 0.0
    yield
    _wait_until(lambda: not _uia_worker_is_running())
    with observer._uia_worker_lock:
        observer._uia_consecutive_timeouts = 0
        observer._uia_circuit_open_until = 0.0


def test_uia_read_is_single_flight_while_timed_out_worker_is_still_running(
    monkeypatch,
) -> None:
    release = threading.Event()
    calls = 0

    def blocked_read() -> WindowText:
        nonlocal calls
        calls += 1
        release.wait(timeout=1.0)
        return WindowText(text_content="late")

    monkeypatch.setattr(observer, "read_active_window", blocked_read)
    monkeypatch.setattr(observer, "_UIA_ISOLATE_TIMEOUT", 0.01)

    try:
        assert observer._read_window_text_isolated().text_content == ""
        assert calls == 1

        started = time.monotonic()
        assert observer._read_window_text_isolated().text_content == ""
        assert time.monotonic() - started < 0.05
        assert calls == 1
    finally:
        release.set()
        _wait_until(lambda: not _uia_worker_is_running())


def test_uia_consecutive_timeouts_open_circuit_until_cooldown_expires(
    monkeypatch,
) -> None:
    calls = 0

    def slow_read() -> WindowText:
        nonlocal calls
        calls += 1
        time.sleep(0.03)
        return WindowText(text_content="late")

    monkeypatch.setattr(observer, "read_active_window", slow_read)
    monkeypatch.setattr(observer, "_UIA_ISOLATE_TIMEOUT", 0.005)
    monkeypatch.setattr(observer, "_UIA_TIMEOUT_CIRCUIT_THRESHOLD", 2)
    monkeypatch.setattr(observer, "_UIA_TIMEOUT_COOLDOWN_SECONDS", 0.04)

    observer._read_window_text_isolated()
    _wait_until(lambda: not _uia_worker_is_running())
    observer._read_window_text_isolated()
    _wait_until(lambda: not _uia_worker_is_running())
    assert calls == 2

    assert observer._read_window_text_isolated().text_content == ""
    assert calls == 2

    time.sleep(0.05)
    observer._read_window_text_isolated()
    assert calls == 3
    _wait_until(lambda: not _uia_worker_is_running())
