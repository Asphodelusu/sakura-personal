"""Windows private-NumPy preload contract for the Plugin API v4 runner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from app.plugins.sakura_plugin_sdk import read_frame, write_frame

_REPO = Path(__file__).resolve().parents[2]
_RUNNER = _REPO / "app" / "plugins" / "plugin_runner_v4.py"
_READ_TIMEOUT_S = 5.0
_EXIT_TIMEOUT_S = 5.0


def _plugin(root: Path, body: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.py").write_text(body, encoding="utf-8")


def _numpy(root: Path, body: str) -> None:
    package = root / "numpy"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(body, encoding="utf-8")


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _command(plugin_root: Path, data_dir: Path, plugin_id: str, dependency_root: Path | None, *, validate: bool) -> list[str]:
    command = [
        sys.executable,
        "-I",
        "-S",
        str(_RUNNER),
        "--plugin-id",
        plugin_id,
        "--generation-id",
        "generation-numpy",
        "--plugin-root",
        str(plugin_root),
        "--data-dir",
        str(data_dir),
        "--entry",
        "plugin:Plugin",
    ]
    if dependency_root is not None:
        command.extend(["--dependency-root", str(dependency_root)])
    if validate:
        command.append("--validate-entry")
    return command


def _popen(command: list[str], cwd: Path) -> subprocess.Popen[bytes]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=_environment(),
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    process._stderr_lines = []  # type: ignore[attr-defined]

    def drain() -> None:
        assert process.stderr is not None
        for raw in iter(process.stderr.readline, b""):
            process._stderr_lines.append(raw.decode("utf-8", "replace"))  # type: ignore[attr-defined]

    threading.Thread(target=drain, daemon=True).start()
    return process


def _request(process: subprocess.Popen[bytes], plugin_id: str, name: str, payload: dict[str, object]) -> dict[str, object]:
    assert process.stdin is not None and process.stdout is not None
    write_frame(
        process.stdin,
        {
            "type": "request",
            "id": uuid.uuid4().hex,
            "generationId": "generation-numpy",
            "pluginId": plugin_id,
            "name": name,
            "payload": payload,
        },
    )
    box: dict[str, object] = {}

    def read() -> None:
        assert process.stdout is not None
        try:
            box["frame"] = read_frame(process.stdout)
        except Exception as error:  # pragma: no cover - failure path surfaces below
            box["error"] = error

    reader = threading.Thread(target=read)
    reader.start()
    reader.join(_READ_TIMEOUT_S)
    if reader.is_alive():
        raise AssertionError(f"{name} timed out pid={process.pid}")
    if "error" in box:
        detail = "".join(getattr(process, "_stderr_lines", []))[-800:]
        raise AssertionError(f"{name} read failed: {box['error']!r} exit={process.poll()} stderr={detail}")
    frame = box["frame"]
    assert isinstance(frame, dict)
    return frame


def _service(process: subprocess.Popen[bytes], plugin_id: str) -> dict[str, object]:
    frame = _request(
        process,
        plugin_id,
        "service.call",
        {
            "serviceKey": "fixture.numpy",
            "method": "info",
            "args": [],
            "callerId": "numpy-startup-test",
        },
    )
    assert frame["ok"] is True
    result = frame["result"]
    assert isinstance(result, dict)
    return result


def _close(process: subprocess.Popen[bytes], plugin_id: str) -> None:
    frame = _request(process, plugin_id, "runtime.close", {})
    assert frame["ok"] is True
    assert process.stdin is not None
    process.stdin.close()
    assert process.wait(timeout=_EXIT_TIMEOUT_S) == 0


def _reap(process: subprocess.Popen[bytes] | None) -> str:
    if process is None:
        return ""
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=_EXIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        pass
    stderr = "".join(getattr(process, "_stderr_lines", []))
    return stderr


def _wait_for(path: Path, process: subprocess.Popen[bytes]) -> str:
    deadline = time.monotonic() + _READ_TIMEOUT_S
    while time.monotonic() < deadline:
        if path.is_file():
            return path.read_text(encoding="utf-8")
        if process.poll() is not None:
            break
        time.sleep(0.02)
    raise AssertionError(f"missing {path.name}; exit={process.poll()}")


@pytest.mark.skipif(os.name != "nt", reason="private NumPy preload is Windows-only")
def test_private_numpy_imports_before_reader_and_is_reused(tmp_path: Path) -> None:
    plugin_id = "fixture.numpy.order"
    plugin_root = tmp_path / "plugin"
    dependency_root = tmp_path / "deps"
    threads = tmp_path / "threads.txt"
    loads = tmp_path / "loads.txt"
    _numpy(
        dependency_root,
        f"""
import threading
from pathlib import Path
ORIGIN = "dependency"
LOADS = Path({str(loads)!r})
previous = int(LOADS.read_text(encoding="utf-8")) if LOADS.exists() else 0
LOADS.write_text(str(previous + 1), encoding="utf-8")
Path({str(threads)!r}).write_text("\\n".join(item.name for item in threading.enumerate()), encoding="utf-8")
""",
    )
    _plugin(
        plugin_root,
        f"""
import sys
from pathlib import Path

class Service:
    def info(self):
        import numpy
        try:
            import app
        except ModuleNotFoundError:
            core_blocked = True
        else:
            core_blocked = False
        dependency = {str(dependency_root)!r}
        return {{
            "origin": numpy.ORIGIN,
            "file": str(Path(numpy.__file__).resolve()),
            "loads": int(Path({str(loads)!r}).read_text(encoding="utf-8")),
            "dependencyCount": sys.path.count(dependency),
            "dependencyFirst": sys.path[0] == dependency,
            "coreBlocked": core_blocked,
        }}

class Plugin:
    def setup(self, context):
        context.provide("fixture.numpy", Service(), exports=("info",))
""",
    )
    process = _popen(
        _command(plugin_root, tmp_path / "data", plugin_id, dependency_root, validate=False),
        tmp_path,
    )
    try:
        observed = _wait_for(threads, process)
        assert f"sakura-plugin-{plugin_id}-reader" not in observed.splitlines()
        assert "MainThread" in observed.splitlines()
        frame = _request(process, plugin_id, "runtime.initialize", {})
        assert frame["ok"] is True
        info = _service(process, plugin_id)
        assert info["origin"] == "dependency"
        assert info["file"] == str((dependency_root / "numpy" / "__init__.py").resolve())
        assert info["loads"] == 1
        assert info["dependencyCount"] == 1
        assert info["dependencyFirst"] is False
        assert info["coreBlocked"] is True
        _close(process, plugin_id)
        assert process.poll() is not None
    finally:
        _reap(process)


@pytest.mark.skipif(os.name != "nt", reason="private NumPy preload is Windows-only")
def test_private_numpy_wins_over_plugin_root_shadow(tmp_path: Path) -> None:
    plugin_id = "fixture.numpy.shadow"
    plugin_root = tmp_path / "plugin"
    dependency_root = tmp_path / "deps"
    plugin_loads = tmp_path / "plugin-loads.txt"
    dependency_loads = tmp_path / "dependency-loads.txt"
    _numpy(
        plugin_root,
        f"""
from pathlib import Path
ORIGIN = "plugin-root"
path = Path({str(plugin_loads)!r})
previous = int(path.read_text(encoding="utf-8")) if path.exists() else 0
path.write_text(str(previous + 1), encoding="utf-8")
""",
    )
    _numpy(
        dependency_root,
        f"""
from pathlib import Path
ORIGIN = "dependency"
path = Path({str(dependency_loads)!r})
previous = int(path.read_text(encoding="utf-8")) if path.exists() else 0
path.write_text(str(previous + 1), encoding="utf-8")
""",
    )
    _plugin(
        plugin_root,
        """
class Service:
    def info(self):
        import numpy
        return {"origin": numpy.ORIGIN, "file": numpy.__file__}

class Plugin:
    def setup(self, context):
        context.provide("fixture.numpy", Service(), exports=("info",))
""",
    )
    process = _popen(
        _command(plugin_root, tmp_path / "data", plugin_id, dependency_root, validate=False),
        tmp_path,
    )
    try:
        _wait_for(dependency_loads, process)
        assert not plugin_loads.exists()
        frame = _request(process, plugin_id, "runtime.initialize", {})
        assert frame["ok"] is True
        info = _service(process, plugin_id)
        assert info["origin"] == "dependency"
        assert Path(str(info["file"])).resolve() == (dependency_root / "numpy" / "__init__.py").resolve()
        assert dependency_loads.read_text(encoding="utf-8") == "1"
        assert not plugin_loads.exists()
        _close(process, plugin_id)
    finally:
        _reap(process)


@pytest.mark.skipif(os.name != "nt", reason="private NumPy preload is Windows-only")
def test_numpy_preload_failure_returns_initialize_error_without_setup(tmp_path: Path) -> None:
    plugin_id = "fixture.numpy.fail"
    plugin_root = tmp_path / "plugin"
    dependency_root = tmp_path / "deps"
    setup_marker = tmp_path / "setup.txt"
    _numpy(dependency_root, 'raise RuntimeError("token=super-secret-value")\n')
    _plugin(
        plugin_root,
        f"""
from pathlib import Path

class Service:
    def info(self):
        return {{"ran": True}}

class Plugin:
    def setup(self, context):
        Path({str(setup_marker)!r}).write_text("ran", encoding="utf-8")
        context.provide("fixture.numpy", Service(), exports=("info",))
""",
    )
    process = _popen(
        _command(plugin_root, tmp_path / "data", plugin_id, dependency_root, validate=False),
        tmp_path,
    )
    try:
        frame = _request(process, plugin_id, "runtime.initialize", {})
        rendered = json.dumps(frame, ensure_ascii=False)
        assert frame["ok"] is False
        error = frame["error"]
        assert isinstance(error, dict)
        assert error["code"] == "PLUGIN_CALL_FAILED"
        assert "super-secret-value" not in rendered
        assert "[REDACTED]" in rendered
        assert not setup_marker.exists()
        assert process.poll() is None
        _close(process, plugin_id)
    finally:
        _reap(process)


def test_missing_private_numpy_starts_normally(tmp_path: Path) -> None:
    plugin_id = "fixture.numpy.absent"
    plugin_root = tmp_path / "plugin"
    dependency_root = tmp_path / "deps"
    dependency_root.mkdir()
    (dependency_root / "other.py").write_text("VALUE = 1\n", encoding="utf-8")
    _plugin(
        plugin_root,
        """
class Service:
    def info(self):
        try:
            import numpy
        except ModuleNotFoundError:
            return {"numpy": "absent"}
        return {"numpy": getattr(numpy, "__file__", "present")}

class Plugin:
    def setup(self, context):
        context.provide("fixture.numpy", Service(), exports=("info",))
""",
    )
    process = _popen(
        _command(plugin_root, tmp_path / "data", plugin_id, dependency_root, validate=False),
        tmp_path,
    )
    try:
        frame = _request(process, plugin_id, "runtime.initialize", {})
        assert frame["ok"] is True
        assert _service(process, plugin_id) == {"numpy": "absent"}
        _close(process, plugin_id)
    finally:
        _reap(process)


def test_validate_entry_does_not_preload_numpy(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugin"
    dependency_root = tmp_path / "deps"
    loads = tmp_path / "loads.txt"
    _numpy(dependency_root, f"from pathlib import Path\nPath({str(loads)!r}).write_text('loaded', encoding='utf-8')\n")
    _plugin(
        plugin_root,
        """
class Plugin:
    def setup(self, context):
        raise AssertionError("setup")
""",
    )
    process = _popen(
        _command(plugin_root, tmp_path / "data", "fixture.numpy.validate", dependency_root, validate=True),
        tmp_path,
    )
    try:
        assert process.wait(timeout=_EXIT_TIMEOUT_S) == 0
        assert not loads.exists()
    finally:
        _reap(process)


def test_non_windows_skips_private_numpy_preload(tmp_path: Path) -> None:
    plugin_id = "fixture.numpy.posix"
    plugin_root = tmp_path / "plugin"
    dependency_root = tmp_path / "deps"
    loads = tmp_path / "loads.txt"
    launcher = tmp_path / "posix_launch.py"
    launcher.write_text(
        "\n".join(
            [
                "import importlib.util",
                "import os",
                "import sys",
                "runner = sys.argv[1]",
                "spec = importlib.util.spec_from_file_location('plugin_runner_v4', runner)",
                "module = importlib.util.module_from_spec(spec)",
                "sys.modules['plugin_runner_v4'] = module",
                "assert spec.loader is not None",
                "spec.loader.exec_module(module)",
                "real_preload = module.PluginRunner._preload_private_native",
                "def preload(self):",
                "    previous = os.name",
                "    os.name = 'posix'",
                "    try:",
                "        return real_preload(self)",
                "    finally:",
                "        os.name = previous",
                "module.PluginRunner._preload_private_native = preload",
                "raise SystemExit(module.main(sys.argv[2:]))",
                "",
            ]
        ),
        encoding="utf-8",
    )
    _numpy(dependency_root, f"from pathlib import Path\nPath({str(loads)!r}).write_text('loaded', encoding='utf-8')\nraise RuntimeError('should-skip')\n")
    _plugin(
        plugin_root,
        """
class Service:
    def info(self):
        return {"ready": True}

class Plugin:
    def setup(self, context):
        context.provide("fixture.numpy", Service(), exports=("info",))
""",
    )
    command = _command(plugin_root, tmp_path / "data", plugin_id, dependency_root, validate=False)
    command = [sys.executable, "-I", "-S", str(launcher), *_command_tail(command)]
    process = _popen(command, tmp_path)
    try:
        frame = _request(process, plugin_id, "runtime.initialize", {})
        assert frame["ok"] is True
        assert _service(process, plugin_id) == {"ready": True}
        assert not loads.exists()
        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=_EXIT_TIMEOUT_S) == 0
    finally:
        _reap(process)


def test_stdin_eof_reaps_runner_without_close_request(tmp_path: Path) -> None:
    plugin_id = "fixture.numpy.eof"
    plugin_root = tmp_path / "plugin"
    _plugin(
        plugin_root,
        """
class Service:
    def info(self):
        return {"ready": True}

class Plugin:
    def setup(self, context):
        context.provide("fixture.numpy", Service(), exports=("info",))
""",
    )
    process = _popen(
        _command(plugin_root, tmp_path / "data", plugin_id, None, validate=False),
        tmp_path,
    )
    try:
        frame = _request(process, plugin_id, "runtime.initialize", {})
        assert frame["ok"] is True
        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=_EXIT_TIMEOUT_S) == 0
        assert process.poll() is not None
    finally:
        _reap(process)


def _command_tail(command: list[str]) -> list[str]:
    return command[3:]
