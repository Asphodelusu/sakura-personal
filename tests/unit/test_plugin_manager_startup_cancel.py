"""Real PluginRuntimeManager close during a confirmed setup RPC wait.

Synthetic stdlib plugin only. This does not accept BGE, desktop, or model startup.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import psutil

from app.plugins.inventory import PluginInventory
from app.plugins.runtime_v4 import (
    CLOSE_TIMEOUT_SECONDS,
    INITIALIZE_TIMEOUT_SECONDS,
    TERMINATE_TIMEOUT_SECONDS,
    PluginRuntimeManager,
)
from app.storage.paths import StoragePaths
from app.storage.runtime_roots import RuntimeRoots

_FIXTURE = '''import os
import time

class Service:
    def ping(self):
        return "ready"

class Plugin:
    def setup(self, context):
        marker = context.data_path("entered")
        temporary = context.data_path("entered.tmp")
        temporary.write_text(str(os.getpid()), encoding="utf-8")
        temporary.replace(marker)
        release = context.data_path("release")
        deadline = time.monotonic() + 15.0
        while not release.is_file():
            if time.monotonic() >= deadline:
                raise TimeoutError("fixture setup was not cancelled")
            time.sleep(0.02)
        context.provide(f"{context.plugin_id}.service", Service(), exports=("ping",))
'''
_EVIDENCE_DIR = Path(__file__).resolve().parents[2] / ".local/verification/manager-startup-cancel"
_MARKER_WAIT_SECONDS = 3.0


def _roots(tmp_path: Path) -> RuntimeRoots:
    distribution = tmp_path / "distribution"
    user = tmp_path / "user"
    (distribution / "plugins" / "builtin").mkdir(parents=True)
    user.mkdir()
    return RuntimeRoots(distribution, user)


def _install(roots: RuntimeRoots, plugin_id: str) -> None:
    root = roots.distribution_root / "plugins" / "builtin" / plugin_id
    root.mkdir(parents=True)
    (root / "plugin.py").write_text(_FIXTURE, encoding="utf-8")
    service_key = f"{plugin_id}.service"
    (root / "plugin.yaml").write_text(
        (
            "api: 4\n"
            f"id: {plugin_id}\n"
            f"name: {plugin_id}\n"
            "version: 1.0.0\n"
            "entry: plugin:Plugin\n"
            f"provides: [{service_key}]\n"
            "requires: []\n"
        ),
        encoding="utf-8",
    )


def _data_dir(roots: RuntimeRoots, plugin_id: str) -> Path:
    return StoragePaths(roots.user_root).plugin_data_for(plugin_id)


def _plugin_record(manager: PluginRuntimeManager, plugin_id: str) -> dict[str, object]:
    return next(
        item
        for item in manager.snapshot()["plugins"]
        if item["pluginId"] == plugin_id
    )


def _thread_names(plugin_id: str) -> list[str]:
    return sorted(
        thread.name
        for thread in threading.enumerate()
        if thread.is_alive() and plugin_id in thread.name
    )


def _wait_until(predicate, timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value is not None:
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.01)


def _wait_entered(data_dir: Path, timeout: float) -> int:
    marker = data_dir / "entered"

    def read_pid() -> int | None:
        try:
            text = marker.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not text.isdecimal():
            return None
        return int(text)

    pid = _wait_until(read_pid, timeout)
    if not isinstance(pid, int):
        raise AssertionError(f"setup entered marker missing: {marker}")
    return pid


def _wait_pid_gone(pid: int, timeout: float) -> None:
    gone = _wait_until(lambda: True if not psutil.pid_exists(pid) else None, timeout)
    assert gone is True, pid


def _wait_threads_gone(plugin_id: str, timeout: float) -> list[str]:
    def empty() -> list[str] | None:
        names = _thread_names(plugin_id)
        return [] if not names else None

    remaining = _wait_until(empty, timeout)
    assert remaining == [], _thread_names(plugin_id)
    return []


def _runner_evidence(pid: int, plugin_id: str) -> dict[str, object]:
    process = psutil.Process(pid)
    command = process.cmdline()
    assert any(part.endswith("plugin_runner_v4.py") for part in command), command
    assert plugin_id in command, command
    ancestors = [parent.pid for parent in process.parents()]
    # Windows venv launchers can add an intermediate interpreter process.
    assert os.getpid() in ancestors, ancestors
    return {"pid": pid, "ppid": process.ppid(), "ancestors": ancestors, "cmdline": command}


def _kill_recorded(pid: int | None, plugin_id: str) -> None:
    if pid is None or pid <= 0 or pid == os.getpid():
        return
    try:
        process = psutil.Process(pid)
        command = process.cmdline()
        if plugin_id not in command or not any(part.endswith("plugin_runner_v4.py") for part in command):
            return
    except psutil.NoSuchProcess:
        return
    process.kill()
    try:
        process.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
    except psutil.TimeoutExpired:
        pass


def _write_evidence(name: str, payload: dict[str, object]) -> None:
    _EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (_EVIDENCE_DIR / name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def test_close_during_confirmed_setup_releases_initialize_wait(tmp_path: Path) -> None:
    plugin_id = "fixture.manager-startup-cancel"
    roots = _roots(tmp_path)
    _install(roots, plugin_id)
    data_dir = _data_dir(roots, plugin_id)
    manager = PluginRuntimeManager(
        roots,
        "generation-manager-startup-cancel",
        PluginInventory(roots).scan().runtime_specs,
    )
    holder: dict[str, object] = {}
    recorded: dict[str, int | None] = {"pid": None}
    evidence: dict[str, object] = {
        "test": "test_close_during_confirmed_setup_releases_initialize_wait",
        "pluginId": plugin_id,
        "wait": "RpcPeer.request runtime.initialize while Plugin.setup blocks on data/release",
    }
    start_thread = threading.Thread(
        target=_capture_start,
        args=(manager, holder),
        name="manager-startup-cancel-start",
        daemon=True,
    )
    try:
        start_thread.start()
        pid = _wait_entered(data_dir, _MARKER_WAIT_SECONDS)
        recorded["pid"] = pid
        evidence["process"] = _runner_evidence(pid, plugin_id)
        threads_during_setup = _thread_names(plugin_id)
        evidence["threadsDuringSetup"] = threads_during_setup
        assert f"sakura-plugin-{plugin_id}-core-reader" in threads_during_setup
        assert f"sakura-plugin-{plugin_id}-process-waiter" in threads_during_setup
        starting = _plugin_record(manager, plugin_id)
        assert starting["reasonCode"] == "PLUGIN_STARTING"
        assert starting["pid"] is None
        assert not (data_dir / "release").exists()

        started = time.monotonic()
        manager.close()
        close_elapsed = time.monotonic() - started
        join_started = time.monotonic()
        start_thread.join(timeout=CLOSE_TIMEOUT_SECONDS)
        join_elapsed = time.monotonic() - join_started
        evidence["closeElapsedSeconds"] = close_elapsed
        evidence["startJoinElapsedSeconds"] = join_elapsed
        evidence["startAliveAfterClose"] = start_thread.is_alive()
        evidence["startError"] = repr(holder.get("error")) if "error" in holder else None
        assert not start_thread.is_alive()
        assert "error" not in holder
        assert close_elapsed < INITIALIZE_TIMEOUT_SECONDS
        snapshot = holder["snapshot"]
        assert isinstance(snapshot, dict)
        closed = next(item for item in snapshot["plugins"] if item["pluginId"] == plugin_id)
        evidence["recordAfterStartReturns"] = closed
        assert closed["state"] == "disabled"
        assert closed["reasonCode"] == "PLUGIN_STOPPED"
        assert closed["pid"] is None
        _wait_pid_gone(pid, TERMINATE_TIMEOUT_SECONDS)
        _wait_threads_gone(plugin_id, TERMINATE_TIMEOUT_SECONDS)
        evidence["pidAliveAfterClose"] = psutil.pid_exists(pid)
        evidence["threadsAfterClose"] = _thread_names(plugin_id)

        repeat_started = time.monotonic()
        manager.close()
        repeat_elapsed = time.monotonic() - repeat_started
        evidence["repeatCloseElapsedSeconds"] = repeat_elapsed
        assert repeat_elapsed < CLOSE_TIMEOUT_SECONDS
        assert _plugin_record(manager, plugin_id)["pid"] is None
        evidence["status"] = "passed"
    except Exception as error:
        evidence["status"] = "failed"
        evidence["failure"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        if start_thread.is_alive():
            manager.close()
            start_thread.join(timeout=INITIALIZE_TIMEOUT_SECONDS)
        _kill_recorded(recorded["pid"], plugin_id)
        evidence["startAliveAfterCleanup"] = start_thread.is_alive()
        evidence["pidAliveAfterCleanup"] = (
            psutil.pid_exists(recorded["pid"]) if isinstance(recorded["pid"], int) else False
        )
        _write_evidence("setup-cancel-evidence.json", evidence)


def test_repeat_close_after_ready_start_reclaims_owned_process(tmp_path: Path) -> None:
    plugin_id = "fixture.manager-startup-cancel-ready"
    roots = _roots(tmp_path)
    _install(roots, plugin_id)
    data_dir = _data_dir(roots, plugin_id)
    data_dir.mkdir(parents=True)
    (data_dir / "release").write_text("go", encoding="utf-8")
    manager = PluginRuntimeManager(
        roots,
        "generation-manager-startup-cancel-ready",
        PluginInventory(roots).scan().runtime_specs,
    )
    recorded: dict[str, int | None] = {"pid": None}
    setup_pid: int | None = None
    evidence: dict[str, object] = {
        "test": "test_repeat_close_after_ready_start_reclaims_owned_process",
        "pluginId": plugin_id,
    }
    try:
        snapshot = manager.start()
        record = next(item for item in snapshot["plugins"] if item["pluginId"] == plugin_id)
        assert record["state"] == "active"
        assert record["reasonCode"] == "READY"
        assert isinstance(record["pid"], int)
        pid = int(record["pid"])
        recorded["pid"] = pid
        evidence["process"] = _runner_evidence(pid, plugin_id)
        setup_pid = _wait_entered(data_dir, _MARKER_WAIT_SECONDS)
        evidence["setupProcess"] = _runner_evidence(setup_pid, plugin_id)
        assert manager.call_service(f"{plugin_id}.service", "ping") == "ready"
        evidence["threadsWhileActive"] = _thread_names(plugin_id)

        started = time.monotonic()
        manager.close()
        close_elapsed = time.monotonic() - started
        evidence["closeElapsedSeconds"] = close_elapsed
        assert close_elapsed < TERMINATE_TIMEOUT_SECONDS
        _wait_pid_gone(pid, TERMINATE_TIMEOUT_SECONDS)
        _wait_pid_gone(setup_pid, TERMINATE_TIMEOUT_SECONDS)
        _wait_threads_gone(plugin_id, TERMINATE_TIMEOUT_SECONDS)
        repeat_started = time.monotonic()
        manager.close()
        repeat_elapsed = time.monotonic() - repeat_started
        evidence["repeatCloseElapsedSeconds"] = repeat_elapsed
        assert repeat_elapsed < CLOSE_TIMEOUT_SECONDS
        closed = _plugin_record(manager, plugin_id)
        evidence["recordAfterRepeatClose"] = closed
        assert closed["pid"] is None
        assert closed["state"] != "active"
        evidence["status"] = "passed"
    except Exception as error:
        evidence["status"] = "failed"
        evidence["failure"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        manager.close()
        _kill_recorded(setup_pid, plugin_id)
        _kill_recorded(recorded["pid"], plugin_id)
        evidence["pidAliveAfterCleanup"] = (
            psutil.pid_exists(recorded["pid"]) if isinstance(recorded["pid"], int) else False
        )
        _write_evidence("repeat-close-evidence.json", evidence)


def _capture_start(manager: PluginRuntimeManager, holder: dict[str, object]) -> None:
    try:
        holder["snapshot"] = manager.start()
    except Exception as error:
        holder["error"] = error
