# -*- coding: utf-8 -*-
"""Tauri RPC 回复必须在进程对象所属线程写 stdin。

会因这些生产缺陷而失败：
- 缺少 ``_queue_rpc_response``，或它在调用线程直接 ``QProcess.write``
- 异步 worker 成功/失败回调仍从 worker 线程调用 ``_send_rpc_response``
- ``memory.search`` 在所属线程执行，或写回后不清理 RPC map
- 同步 ``_send_rpc_response`` 的 marker / payload 形状被改掉
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock

import pytest

from tests.support.pyside6_stub import is_pyside6_stub_active

pytest.importorskip("PySide6.QtWidgets")

if is_pyside6_stub_active():
    pytest.skip("需要真实 PySide6 事件循环与后台线程", allow_module_level=True)

from PySide6.QtCore import QCoreApplication, QDeadlineTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.tauri_settings import (  # noqa: E402
    TAURI_SETTINGS_RPC_RESULT_MARKER,
    TauriSettingsProcess,
    _shutdown_rpc_maps,
)
from app.ui.tauri_studio import (  # noqa: E402
    TAURI_STUDIO_RPC_RESULT_MARKER,
    TauriStudioProcess,
    dispatch_tauri_studio_rpc,
)


class RecordingProcess:
    """记录每次 write 的调用线程与原始字节，不启动 Tauri 二进制。"""

    def __init__(self) -> None:
        self.writes: list[tuple[int, bytes]] = []

    def write(self, data: bytes) -> int:
        blob = bytes(data)
        self.writes.append((threading.get_ident(), blob))
        return len(blob)


class RecordingMemoryStore:
    def __init__(self, *, result: dict[str, Any] | None = None, error: str = "") -> None:
        self.result = result or {
            "status": "ok",
            "memories": [{"id": "mem-1", "content": "喜欢猫"}],
        }
        self.error = error
        self.search_thread_ident: int | None = None
        self.search_arguments: dict[str, Any] | None = None

    def search_memory(self, arguments: dict[str, Any], wait: bool = True) -> dict[str, Any]:
        del wait
        self.search_thread_ident = threading.get_ident()
        self.search_arguments = dict(arguments)
        if self.error:
            raise RuntimeError(self.error)
        return dict(self.result)


def _qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    assert isinstance(app, QApplication)
    return app


def _spin_until(predicate: Callable[[], bool], timeout_ms: int = 3000) -> None:
    deadline = QDeadlineTimer(timeout_ms)
    while not predicate() and not deadline.hasExpired():
        QCoreApplication.processEvents()
        # The workers execute Python in a QThread. Yield the GIL so this polling
        # loop cannot starve them and turn cleanup into a native Qt race.
        time.sleep(0.001)
    if not predicate():
        raise AssertionError(f"条件在 {timeout_ms}ms 内未满足")


def _parse_rpc_write(raw: bytes, marker: str) -> dict[str, Any]:
    text = raw.decode("utf-8")
    assert text.startswith(marker), text
    assert text.endswith("\n"), text
    payload = json.loads(text[len(marker) :].strip())
    assert isinstance(payload, dict)
    return payload


def _invoke_from_python_thread(func: Callable[[], None]) -> None:
    errors: list[BaseException] = []

    def runner() -> None:
        try:
            func()
        except BaseException as exc:  # noqa: BLE001 - 把工作线程异常带回测试线程
            errors.append(exc)

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    if errors:
        raise errors[0]


def _shutdown_settings(process: TauriSettingsProcess) -> None:
    process._process = None
    _shutdown_rpc_maps(
        (
            process._api_probes,
            process._memory_rpcs,
            process._character_rpcs,
            process._theme_ai_rpcs,
            process._tts_test_rpcs,
        ),
        total_wait_ms=1000,
    )


def _shutdown_studio(process: TauriStudioProcess) -> None:
    process._process = None
    _shutdown_rpc_maps((process._rpcs,), total_wait_ms=1000)


@contextmanager
def _settings_host(
    tmp_path: Path,
    *,
    memory_store: object | None = None,
) -> Iterator[tuple[QApplication, TauriSettingsProcess, RecordingProcess]]:
    app = _qt_app()
    recorder = RecordingProcess()
    process = TauriSettingsProcess(
        base_dir=tmp_path,
        settings=MagicMock(),
        memory_store=memory_store,
    )
    process._process = recorder  # type: ignore[assignment]
    process._done = False
    try:
        yield app, process, recorder
    finally:
        _shutdown_settings(process)


@contextmanager
def _studio_host(
    tmp_path: Path,
) -> Iterator[tuple[QApplication, TauriStudioProcess, RecordingProcess]]:
    app = _qt_app()
    recorder = RecordingProcess()
    process = TauriStudioProcess(tmp_path)
    process._process = recorder  # type: ignore[assignment]
    process._done = False
    try:
        yield app, process, recorder
    finally:
        _shutdown_studio(process)


def test_settings_queue_helper_from_worker_writes_once_on_owner_thread(tmp_path: Path) -> None:
    """``_queue_rpc_response`` 若在 Python 工作线程直接 write，本测试失败。"""
    with _settings_host(tmp_path) as (_app, process, recorder):
        owner_ident = threading.get_ident()
        queue = getattr(process, "_queue_rpc_response", None)
        assert callable(queue), "TauriSettingsProcess 缺少 _queue_rpc_response"
        _invoke_from_python_thread(
            lambda: queue(
                "req-settings-queue",
                ok=True,
                result={"queued": True},
            )
        )
        _spin_until(lambda: len(recorder.writes) >= 1)
        assert len(recorder.writes) == 1
        write_ident, raw = recorder.writes[0]
        assert write_ident == owner_ident
        payload = _parse_rpc_write(raw, TAURI_SETTINGS_RPC_RESULT_MARKER)
        assert payload == {
            "id": "req-settings-queue",
            "ok": True,
            "result": {"queued": True},
        }


def test_studio_queue_helper_from_worker_writes_once_on_owner_thread(tmp_path: Path) -> None:
    """Studio 侧 queue helper 必须把唯一一次 write 投递到所属线程。"""
    with _studio_host(tmp_path) as (_app, process, recorder):
        owner_ident = threading.get_ident()
        queue = getattr(process, "_queue_rpc_response", None)
        assert callable(queue), "TauriStudioProcess 缺少 _queue_rpc_response"
        _invoke_from_python_thread(
            lambda: queue(
                "req-studio-queue",
                ok=True,
                result={"queued": True},
            )
        )
        _spin_until(lambda: len(recorder.writes) >= 1)
        assert len(recorder.writes) == 1
        write_ident, raw = recorder.writes[0]
        assert write_ident == owner_ident
        payload = _parse_rpc_write(raw, TAURI_STUDIO_RPC_RESULT_MARKER)
        assert payload == {
            "id": "req-studio-queue",
            "ok": True,
            "result": {"queued": True},
            "error": "",
        }


def test_settings_memory_search_runs_off_owner_and_writes_on_owner(tmp_path: Path) -> None:
    """真实 ``memory.search`` 分发：检索在后台，stdin 写回与 map 清理在所属线程。"""
    store = RecordingMemoryStore()
    with _settings_host(tmp_path, memory_store=store) as (_app, process, recorder):
        owner_ident = threading.get_ident()
        process._handle_rpc_request(
            json.dumps(
                {
                    "id": "req-mem-search",
                    "method": "memory.search",
                    "params": {"query": "cats"},
                }
            )
        )
        _spin_until(
            lambda: bool(recorder.writes) and "req-mem-search" not in process._memory_rpcs
        )
        assert len(recorder.writes) == 1
        assert store.search_thread_ident is not None
        assert store.search_thread_ident != owner_ident
        assert store.search_arguments is not None
        assert store.search_arguments["query"] == "cats"
        assert store.search_arguments["limit"] == 120
        write_ident, raw = recorder.writes[0]
        assert write_ident == owner_ident
        payload = _parse_rpc_write(raw, TAURI_SETTINGS_RPC_RESULT_MARKER)
        assert payload == {
            "id": "req-mem-search",
            "ok": True,
            "result": {
                "status": "ok",
                "memories": [{"id": "mem-1", "content": "喜欢猫"}],
            },
        }
        assert "req-mem-search" not in process._memory_rpcs


def test_studio_async_success_writes_on_owner_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Studio 异步成功路径必须经所属线程写出，且 dispatch 本身不在所属线程。"""
    dispatch_idents: list[int] = []
    real_dispatch = dispatch_tauri_studio_rpc

    def _spy_dispatch(base_dir: Path, method: str, params: dict[str, Any]) -> dict[str, Any]:
        dispatch_idents.append(threading.get_ident())
        return real_dispatch(base_dir, method, params)

    monkeypatch.setattr("app.ui.tauri_studio.dispatch_tauri_studio_rpc", _spy_dispatch)

    with _studio_host(tmp_path) as (_app, process, recorder):
        owner_ident = threading.get_ident()
        process._handle_rpc_request(
            json.dumps(
                {
                    "id": "req-studio-ok",
                    "method": "studio.list_characters",
                    "params": {},
                }
            )
        )
        _spin_until(lambda: bool(recorder.writes) and "req-studio-ok" not in process._rpcs)
        assert len(recorder.writes) == 1
        assert len(dispatch_idents) == 1
        assert dispatch_idents[0] != owner_ident
        write_ident, raw = recorder.writes[0]
        assert write_ident == owner_ident
        payload = _parse_rpc_write(raw, TAURI_STUDIO_RPC_RESULT_MARKER)
        assert payload["id"] == "req-studio-ok"
        assert payload["ok"] is True
        assert payload["error"] == ""
        assert payload["result"] == {"characters": []}


def test_studio_async_failure_preserves_error_and_writes_on_owner_thread(tmp_path: Path) -> None:
    """异步失败必须保留错误文案，并且同样在所属线程 write。"""
    with _studio_host(tmp_path) as (_app, process, recorder):
        owner_ident = threading.get_ident()
        process._handle_rpc_request(
            json.dumps(
                {
                    "id": "req-studio-fail",
                    "method": "studio.not_a_real_method",
                    "params": {},
                }
            )
        )
        _spin_until(
            lambda: bool(recorder.writes) and "req-studio-fail" not in process._rpcs
        )
        assert len(recorder.writes) == 1
        write_ident, raw = recorder.writes[0]
        assert write_ident == owner_ident
        payload = _parse_rpc_write(raw, TAURI_STUDIO_RPC_RESULT_MARKER)
        assert payload == {
            "id": "req-studio-fail",
            "ok": False,
            "result": None,
            "error": "未知 Tauri Studio RPC 方法：studio.not_a_real_method",
        }


def test_settings_async_failure_preserves_error_and_writes_on_owner_thread(tmp_path: Path) -> None:
    """Settings 默认失败回调也必须排队到所属线程，不能丢掉 error。"""
    store = RecordingMemoryStore(error="qdrant down")
    with _settings_host(tmp_path, memory_store=store) as (_app, process, recorder):
        owner_ident = threading.get_ident()
        process._handle_rpc_request(
            json.dumps(
                {
                    "id": "req-mem-fail",
                    "method": "memory.search",
                    "params": {"query": "cats"},
                }
            )
        )
        _spin_until(lambda: bool(recorder.writes) and "req-mem-fail" not in process._memory_rpcs)
        assert len(recorder.writes) == 1
        assert store.search_thread_ident is not None
        assert store.search_thread_ident != owner_ident
        write_ident, raw = recorder.writes[0]
        assert write_ident == owner_ident
        payload = _parse_rpc_write(raw, TAURI_SETTINGS_RPC_RESULT_MARKER)
        assert payload == {
            "id": "req-mem-fail",
            "ok": False,
            "error": "qdrant down",
        }


def test_settings_sync_send_rpc_response_writes_on_owner_thread(tmp_path: Path) -> None:
    """同步路径继续直接 ``_send_rpc_response``，所属线程立刻写出原有 payload。"""
    with _settings_host(tmp_path) as (_app, process, recorder):
        owner_ident = threading.get_ident()
        process._send_rpc_response("req-settings-sync", ok=True, result={"applied": True})
        assert len(recorder.writes) == 1
        write_ident, raw = recorder.writes[0]
        assert write_ident == owner_ident
        payload = _parse_rpc_write(raw, TAURI_SETTINGS_RPC_RESULT_MARKER)
        assert payload == {
            "id": "req-settings-sync",
            "ok": True,
            "result": {"applied": True},
        }


def test_studio_sync_send_rpc_response_writes_on_owner_thread(tmp_path: Path) -> None:
    """Studio 同步 ``_send_rpc_response`` 保持现有 payload 形状。"""
    with _studio_host(tmp_path) as (_app, process, recorder):
        owner_ident = threading.get_ident()
        process._send_rpc_response("req-studio-sync", ok=False, error="RPC 请求缺少 method。")
        assert len(recorder.writes) == 1
        write_ident, raw = recorder.writes[0]
        assert write_ident == owner_ident
        payload = _parse_rpc_write(raw, TAURI_STUDIO_RPC_RESULT_MARKER)
        assert payload == {
            "id": "req-studio-sync",
            "ok": False,
            "result": None,
            "error": "RPC 请求缺少 method。",
        }
