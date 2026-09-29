"""Synthetic Windows path contracts for plugin processes. No NumPy or personal data."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.plugins.dependencies import PluginDependencyRoots
from app.plugins.models import PluginSpec
from app.plugins.runtime_v4 import _PluginProcess
from app.storage.runtime_roots import RuntimeRoots


RUNTIME_PYTHON = Path(__file__).resolve().parents[2] / "runtime" / "python.exe"
PLUGINS_DIR = Path(__file__).resolve().parents[2] / "app" / "plugins"


def _marker(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".sakura-dependencies.json").write_text(
        json.dumps({
            "schemaVersion": 1,
            "kind": "requirements.txt",
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        }),
        encoding="utf-8",
    )


def test_builtin_and_user_dependency_roots_stay_distinct(tmp_path: Path) -> None:
    plugin_id = "fixture.builtin"
    plugin_root = tmp_path / "distribution" / "plugins" / "builtin" / plugin_id
    plugin_root.mkdir(parents=True)
    (plugin_root / "requirements.txt").write_text("numpy\n", encoding="utf-8")
    bundled = tmp_path / "distribution" / "plugins" / "dependencies" / plugin_id
    user = tmp_path / "user" / "data" / "plugin-runtime" / "dependencies" / plugin_id
    _marker(bundled)
    _marker(user)
    roots = PluginDependencyRoots(tmp_path / "user", distribution_root=tmp_path / "distribution")

    assert roots.verified_root(plugin_id, plugin_root, source="bundled") == bundled
    assert roots.verified_root(plugin_id, plugin_root, source="user") == user
    assert bundled != user


@pytest.mark.skipif(os.name != "nt", reason="Windows extended path syntax")
def test_process_path_strips_only_extended_drive_and_unc() -> None:
    from app.plugins.process_paths import process_path

    assert process_path(r"\\?\D:\plugins\mem0") == r"D:\plugins\mem0"
    assert process_path(r"\\?\UNC\server\share\deps") == r"\\server\share\deps"
    assert process_path(r"D:\plugins\mem0") == r"D:\plugins\mem0"
    assert process_path(r"\\server\share\deps") == r"\\server\share\deps"
    assert process_path(r"\\?\Volume{01234567-89ab-cdef-0123-456789abcdef}\deps") == (
        r"\\?\Volume{01234567-89ab-cdef-0123-456789abcdef}\deps"
    )


def test_process_path_is_unchanged_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.plugins import process_paths

    # Replace this module's OS reference, not the shared os.name used by pathlib.
    monkeypatch.setattr(process_paths, "os", SimpleNamespace(name="posix"))
    for value in ("/tmp/plugins/mem0", r"\\?\D:\plugins\mem0", r"\\?\UNC\server\share\deps"):
        assert process_paths.process_path(value) == value


@pytest.mark.skipif(os.name != "nt", reason="Windows process and extended path contract")
def test_runner_import_root_drops_extended_prefix_before_preload(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugin"
    dependency_root = tmp_path / "deps"
    data_dir = tmp_path / "data"
    for path in (plugin_root, dependency_root, data_dir):
        path.mkdir()
    extended_plugin = "\\\\?\\" + str(plugin_root.resolve())
    extended_dependency = "\\\\?\\" + str(dependency_root.resolve())
    extended_data = "\\\\?\\" + str(data_dir.resolve())
    result_path = tmp_path / "boundary.json"
    child_code = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from plugin_runner_v4 import PluginRunner
runner = PluginRunner(
    plugin_id="probe.boundary",
    generation_id="probe",
    plugin_root=Path(sys.argv[2]),
    dependency_root=Path(sys.argv[3]),
    data_dir=Path(sys.argv[4]),
    entry="plugin:Plugin",
)
runner._prepare_import_path()
Path(sys.argv[5]).write_text(json.dumps({
    "plugin_root": str(runner.plugin_root),
    "dependency_root": str(runner.dependency_root),
    "data_dir": str(runner.data_dir),
    "sys_path": sys.path,
}), encoding="utf-8")
"""
    process = subprocess.Popen(
        [
            str(RUNTIME_PYTHON),
            "-I",
            "-S",
            "-c",
            child_code,
            str(PLUGINS_DIR),
            extended_plugin,
            extended_dependency,
            extended_data,
            str(result_path),
        ],
        cwd=extended_data,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=5)
        raise
    assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
    observed = json.loads(result_path.read_text(encoding="utf-8"))
    assert observed["plugin_root"] == str(plugin_root.resolve())
    assert observed["dependency_root"] == str(dependency_root.resolve())
    assert observed["data_dir"] == str(data_dir.resolve())
    assert extended_dependency not in observed["sys_path"]
    assert str(dependency_root.resolve()) in observed["sys_path"]
    assert not stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows process and extended path contract")
def test_plugin_process_cwd_drops_extended_user_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user = tmp_path / "user"
    user.mkdir()
    distribution = tmp_path / "distribution"
    distribution.mkdir()
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    extended_user = "\\\\?\\" + str(user.resolve())
    captured: dict[str, object] = {}

    def stop_at_spawn(command: list[str], **kwargs: object) -> None:
        captured["cwd"] = kwargs.get("cwd")
        captured["command"] = command
        raise OSError("synthetic stop before a plugin process exists")

    monkeypatch.setattr("app.plugins.runtime_v4.subprocess.Popen", stop_at_spawn)
    spec = PluginSpec(
        entry="plugin:Plugin",
        plugin_id="fixture.builtin",
        plugin_root=plugin_root,
        source="bundled",
    )
    process = _PluginProcess(
        roots=RuntimeRoots(distribution, Path(extended_user)),
        generation_id="probe",
        spec=spec,
        dependency_root=tmp_path / "deps",
        request_handler=lambda _name, _payload: None,
        on_exit=lambda _plugin_id, _process: None,
        call_timeout=0.1,
    )
    with pytest.raises(Exception):
        process.start()
    cwd = str(captured["cwd"])
    assert not cwd.startswith("\\\\?\\")
    assert Path(cwd).is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows process and extended path contract")
def test_dependency_validation_cwd_drops_extended_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    extended = "\\\\?\\" + str(plugin_root.resolve())
    captured: dict[str, object] = {}

    def stop_at_run(command: list[str], **kwargs: object) -> object:
        captured["cwd"] = kwargs.get("cwd")
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("app.plugins.dependencies.subprocess.run", stop_at_run)
    roots = PluginDependencyRoots(tmp_path / "user", python=RUNTIME_PYTHON)
    roots._validate_entry("fixture.builtin", Path(extended), None, "plugin:Plugin")
    assert not str(captured["cwd"]).startswith("\\\\?\\")
    assert Path(str(captured["cwd"])).resolve() == plugin_root.resolve()
