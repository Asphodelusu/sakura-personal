"""Windows private SciPy linalg preload contract for the Plugin API v4 runner."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_SUPPORT_PATH = Path(__file__).with_name("test_plugin_numpy_startup.py")
_SUPPORT_SPEC = importlib.util.spec_from_file_location("plugin_numpy_startup_support", _SUPPORT_PATH)
assert _SUPPORT_SPEC is not None and _SUPPORT_SPEC.loader is not None
_support = importlib.util.module_from_spec(_SUPPORT_SPEC)
_SUPPORT_SPEC.loader.exec_module(_support)


def _scipy(root: Path, package_body: str, linalg_body: str | None = None) -> None:
    package = root / "scipy"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(package_body, encoding="utf-8")
    if linalg_body is None:
        return
    linalg = package / "linalg"
    linalg.mkdir()
    (linalg / "__init__.py").write_text(linalg_body, encoding="utf-8")


def _service(process: object, plugin_id: str) -> dict[str, object]:
    frame = _support._request(
        process,
        plugin_id,
        "service.call",
        {
            "serviceKey": "fixture.scipy",
            "method": "info",
            "args": [],
            "callerId": "scipy-startup-test",
        },
    )
    assert frame["ok"] is True
    result = frame["result"]
    assert isinstance(result, dict)
    return result


def _record(path: Path, label: str) -> str:
    return f"""
from pathlib import Path
_path = Path({str(path)!r})
_previous = _path.read_text(encoding="utf-8") if _path.exists() else ""
_path.write_text(_previous + {label!r} + "\\n", encoding="utf-8")
"""


def _counter(path: Path) -> str:
    return f"""
from pathlib import Path
_loads = Path({str(path)!r})
_count = int(_loads.read_text(encoding="utf-8")) if _loads.exists() else 0
_loads.write_text(str(_count + 1), encoding="utf-8")
"""


@pytest.mark.skipif(os.name != "nt", reason="private SciPy preload is Windows-only")
def test_private_scipy_linalg_imports_before_reader_and_is_reused(tmp_path: Path) -> None:
    plugin_id = "fixture.scipy.order"
    plugin_root = tmp_path / "plugin"
    dependency_root = tmp_path / "deps"
    order = tmp_path / "order.txt"
    threads = tmp_path / "threads.txt"
    numpy_loads = tmp_path / "numpy-loads.txt"
    scipy_loads = tmp_path / "scipy-loads.txt"
    linalg_loads = tmp_path / "linalg-loads.txt"
    _support._numpy(
        dependency_root,
        _record(order, "numpy") + _counter(numpy_loads) + "ORIGIN = 'dependency-numpy'\n",
    )
    _scipy(
        dependency_root,
        _record(order, "scipy")
        + _counter(scipy_loads)
        + "ORIGIN = 'dependency-scipy'\nimport numpy\n",
        _record(order, "scipy.linalg")
        + _counter(linalg_loads)
        + f"""
import threading
from pathlib import Path
ORIGIN = "dependency-linalg"
Path({str(threads)!r}).write_text("\\n".join(item.name for item in threading.enumerate()), encoding="utf-8")
""",
    )
    _support._plugin(
        plugin_root,
        f"""
import sys
from pathlib import Path

class Service:
    def info(self):
        import numpy
        import scipy
        import scipy.linalg
        try:
            import app
        except ModuleNotFoundError:
            core_blocked = True
        else:
            core_blocked = False
        dependency = {str(dependency_root)!r}
        return {{
            "numpyOrigin": numpy.ORIGIN,
            "numpyFile": str(Path(numpy.__file__).resolve()),
            "numpyLoads": int(Path({str(numpy_loads)!r}).read_text(encoding="utf-8")),
            "scipyOrigin": scipy.ORIGIN,
            "scipyFile": str(Path(scipy.__file__).resolve()),
            "scipyLoads": int(Path({str(scipy_loads)!r}).read_text(encoding="utf-8")),
            "linalgOrigin": scipy.linalg.ORIGIN,
            "linalgFile": str(Path(scipy.linalg.__file__).resolve()),
            "linalgLoads": int(Path({str(linalg_loads)!r}).read_text(encoding="utf-8")),
            "order": Path({str(order)!r}).read_text(encoding="utf-8"),
            "dependencyCount": sys.path.count(dependency),
            "dependencyFirst": sys.path[0] == dependency,
            "coreBlocked": core_blocked,
        }}

class Plugin:
    def setup(self, context):
        context.provide("fixture.scipy", Service(), exports=("info",))
""",
    )
    process = _support._popen(
        _support._command(plugin_root, tmp_path / "data", plugin_id, dependency_root, validate=False),
        tmp_path,
    )
    try:
        observed = _support._wait_for(threads, process)
        assert f"sakura-plugin-{plugin_id}-reader" not in observed.splitlines()
        assert "MainThread" in observed.splitlines()
        frame = _support._request(process, plugin_id, "runtime.initialize", {})
        assert frame["ok"] is True
        info = _service(process, plugin_id)
        assert info["order"] == "numpy\nscipy\nscipy.linalg\n"
        assert info["numpyOrigin"] == "dependency-numpy"
        assert info["scipyOrigin"] == "dependency-scipy"
        assert info["linalgOrigin"] == "dependency-linalg"
        assert info["numpyFile"] == str((dependency_root / "numpy" / "__init__.py").resolve())
        assert info["scipyFile"] == str((dependency_root / "scipy" / "__init__.py").resolve())
        assert info["linalgFile"] == str((dependency_root / "scipy" / "linalg" / "__init__.py").resolve())
        assert info["numpyLoads"] == 1
        assert info["scipyLoads"] == 1
        assert info["linalgLoads"] == 1
        assert info["dependencyCount"] == 1
        assert info["dependencyFirst"] is False
        assert info["coreBlocked"] is True
        _support._close(process, plugin_id)
    finally:
        _support._reap(process)


@pytest.mark.skipif(os.name != "nt", reason="private SciPy preload is Windows-only")
def test_private_scipy_linalg_wins_over_plugin_root_shadow(tmp_path: Path) -> None:
    plugin_id = "fixture.scipy.shadow"
    plugin_root = tmp_path / "plugin"
    dependency_root = tmp_path / "deps"
    plugin_package = tmp_path / "plugin-scipy.txt"
    plugin_linalg = tmp_path / "plugin-linalg.txt"
    dependency_package = tmp_path / "dependency-scipy.txt"
    dependency_linalg = tmp_path / "dependency-linalg.txt"
    _scipy(
        plugin_root,
        _counter(plugin_package) + "ORIGIN = 'plugin-scipy'\n",
        _counter(plugin_linalg) + "ORIGIN = 'plugin-linalg'\n",
    )
    _scipy(
        dependency_root,
        _counter(dependency_package) + "ORIGIN = 'dependency-scipy'\n",
        _counter(dependency_linalg) + "ORIGIN = 'dependency-linalg'\n",
    )
    _support._plugin(
        plugin_root,
        """
class Service:
    def info(self):
        import scipy
        import scipy.linalg
        return {
            "scipyOrigin": scipy.ORIGIN,
            "scipyFile": scipy.__file__,
            "linalgOrigin": scipy.linalg.ORIGIN,
            "linalgFile": scipy.linalg.__file__,
        }

class Plugin:
    def setup(self, context):
        context.provide("fixture.scipy", Service(), exports=("info",))
""",
    )
    process = _support._popen(
        _support._command(plugin_root, tmp_path / "data", plugin_id, dependency_root, validate=False),
        tmp_path,
    )
    try:
        _support._wait_for(dependency_linalg, process)
        assert not plugin_package.exists()
        assert not plugin_linalg.exists()
        frame = _support._request(process, plugin_id, "runtime.initialize", {})
        assert frame["ok"] is True
        info = _service(process, plugin_id)
        assert info["scipyOrigin"] == "dependency-scipy"
        assert info["linalgOrigin"] == "dependency-linalg"
        assert Path(str(info["scipyFile"])).resolve() == (dependency_root / "scipy" / "__init__.py").resolve()
        assert Path(str(info["linalgFile"])).resolve() == (
            dependency_root / "scipy" / "linalg" / "__init__.py"
        ).resolve()
        assert dependency_package.read_text(encoding="utf-8") == "1"
        assert dependency_linalg.read_text(encoding="utf-8") == "1"
        assert not plugin_package.exists()
        assert not plugin_linalg.exists()
        _support._close(process, plugin_id)
    finally:
        _support._reap(process)


@pytest.mark.skipif(os.name != "nt", reason="private SciPy preload is Windows-only")
def test_scipy_preload_failure_returns_initialize_error_without_setup(tmp_path: Path) -> None:
    plugin_id = "fixture.scipy.fail"
    plugin_root = tmp_path / "plugin"
    dependency_root = tmp_path / "deps"
    setup_marker = tmp_path / "setup.txt"
    _scipy(
        dependency_root,
        "ORIGIN = 'dependency-scipy'\n",
        'raise RuntimeError("token=super-secret-value")\n',
    )
    _support._plugin(
        plugin_root,
        f"""
from pathlib import Path

class Service:
    def info(self):
        return {{"ran": True}}

class Plugin:
    def setup(self, context):
        Path({str(setup_marker)!r}).write_text("ran", encoding="utf-8")
        context.provide("fixture.scipy", Service(), exports=("info",))
""",
    )
    process = _support._popen(
        _support._command(plugin_root, tmp_path / "data", plugin_id, dependency_root, validate=False),
        tmp_path,
    )
    try:
        frame = _support._request(process, plugin_id, "runtime.initialize", {})
        rendered = json.dumps(frame, ensure_ascii=False)
        assert frame["ok"] is False
        error = frame["error"]
        assert isinstance(error, dict)
        assert error["code"] == "PLUGIN_CALL_FAILED"
        assert "super-secret-value" not in rendered
        assert "[REDACTED]" in rendered
        assert not setup_marker.exists()
        assert process.poll() is None
        _support._close(process, plugin_id)
    finally:
        _support._reap(process)


@pytest.mark.skipif(os.name != "nt", reason="private SciPy preload is Windows-only")
def test_scipy_preloads_when_private_numpy_is_absent(tmp_path: Path) -> None:
    plugin_id = "fixture.scipy.no-numpy"
    plugin_root = tmp_path / "plugin"
    dependency_root = tmp_path / "deps"
    loads = tmp_path / "linalg-loads.txt"
    _scipy(
        dependency_root,
        "ORIGIN = 'dependency-scipy'\n",
        _counter(loads) + "ORIGIN = 'dependency-linalg'\n",
    )
    _support._plugin(
        plugin_root,
        f"""
from pathlib import Path

class Service:
    def info(self):
        import scipy
        import scipy.linalg
        try:
            import numpy
        except ModuleNotFoundError:
            numpy_state = "absent"
        else:
            numpy_state = getattr(numpy, "__file__", "present")
        return {{
            "scipyFile": str(Path(scipy.__file__).resolve()),
            "linalgFile": str(Path(scipy.linalg.__file__).resolve()),
            "loads": int(Path({str(loads)!r}).read_text(encoding="utf-8")),
            "numpy": numpy_state,
        }}

class Plugin:
    def setup(self, context):
        context.provide("fixture.scipy", Service(), exports=("info",))
""",
    )
    process = _support._popen(
        _support._command(plugin_root, tmp_path / "data", plugin_id, dependency_root, validate=False),
        tmp_path,
    )
    try:
        _support._wait_for(loads, process)
        frame = _support._request(process, plugin_id, "runtime.initialize", {})
        assert frame["ok"] is True
        info = _service(process, plugin_id)
        assert info["scipyFile"] == str((dependency_root / "scipy" / "__init__.py").resolve())
        assert info["linalgFile"] == str((dependency_root / "scipy" / "linalg" / "__init__.py").resolve())
        assert info["loads"] == 1
        assert info["numpy"] == "absent"
        _support._close(process, plugin_id)
    finally:
        _support._reap(process)


@pytest.mark.skipif(os.name != "nt", reason="private SciPy preload is Windows-only")
def test_missing_private_scipy_skips_plugin_shadow(tmp_path: Path) -> None:
    plugin_id = "fixture.scipy.absent"
    plugin_root = tmp_path / "plugin"
    dependency_root = tmp_path / "deps"
    shadow = tmp_path / "shadow.txt"
    dependency_root.mkdir()
    _scipy(
        plugin_root,
        _counter(shadow) + "ORIGIN = 'plugin-scipy'\n",
        "ORIGIN = 'plugin-linalg'\n",
    )
    _support._plugin(
        plugin_root,
        """
class Service:
    def info(self):
        return {"ready": True}

class Plugin:
    def setup(self, context):
        context.provide("fixture.scipy", Service(), exports=("info",))
""",
    )
    process = _support._popen(
        _support._command(plugin_root, tmp_path / "data", plugin_id, dependency_root, validate=False),
        tmp_path,
    )
    try:
        frame = _support._request(process, plugin_id, "runtime.initialize", {})
        assert frame["ok"] is True
        assert _service(process, plugin_id) == {"ready": True}
        assert not shadow.exists()
        _support._close(process, plugin_id)
    finally:
        _support._reap(process)


@pytest.mark.skipif(os.name != "nt", reason="private SciPy preload is Windows-only")
def test_missing_scipy_linalg_init_skips_parent_import(tmp_path: Path) -> None:
    plugin_id = "fixture.scipy.no-linalg"
    plugin_root = tmp_path / "plugin"
    dependency_root = tmp_path / "deps"
    parent = tmp_path / "parent.txt"
    shadow = tmp_path / "shadow-linalg.txt"
    _scipy(dependency_root, _counter(parent) + "ORIGIN = 'dependency-scipy'\n")
    _scipy(plugin_root, "ORIGIN = 'plugin-scipy'\n", _counter(shadow) + "ORIGIN = 'plugin-linalg'\n")
    _support._plugin(
        plugin_root,
        """
class Service:
    def info(self):
        return {"ready": True}

class Plugin:
    def setup(self, context):
        context.provide("fixture.scipy", Service(), exports=("info",))
""",
    )
    process = _support._popen(
        _support._command(plugin_root, tmp_path / "data", plugin_id, dependency_root, validate=False),
        tmp_path,
    )
    try:
        frame = _support._request(process, plugin_id, "runtime.initialize", {})
        assert frame["ok"] is True
        assert _service(process, plugin_id) == {"ready": True}
        assert not parent.exists()
        assert not shadow.exists()
        _support._close(process, plugin_id)
    finally:
        _support._reap(process)


@pytest.mark.skipif(os.name != "nt", reason="private SciPy preload is Windows-only")
def test_scipy_linalg_rejects_cached_wrong_origin(tmp_path: Path) -> None:
    plugin_id = "fixture.scipy.cached"
    plugin_root = tmp_path / "plugin"
    dependency_root = tmp_path / "deps"
    setup_marker = tmp_path / "setup.txt"
    real_linalg = tmp_path / "real-linalg.txt"
    _scipy(
        dependency_root,
        """
import sys
import types
ORIGIN = "dependency-scipy"
cached = types.ModuleType("scipy.linalg")
cached.__file__ = r"C:\\scipy-shadow\\scipy\\linalg\\__init__.py"
cached.ORIGIN = "cached-linalg"
sys.modules["scipy.linalg"] = cached
""",
        _counter(real_linalg) + "ORIGIN = 'dependency-linalg'\n",
    )
    _support._plugin(
        plugin_root,
        f"""
from pathlib import Path

class Service:
    def info(self):
        return {{"ran": True}}

class Plugin:
    def setup(self, context):
        Path({str(setup_marker)!r}).write_text("ran", encoding="utf-8")
        context.provide("fixture.scipy", Service(), exports=("info",))
""",
    )
    process = _support._popen(
        _support._command(plugin_root, tmp_path / "data", plugin_id, dependency_root, validate=False),
        tmp_path,
    )
    try:
        frame = _support._request(process, plugin_id, "runtime.initialize", {})
        assert frame["ok"] is False
        error = frame["error"]
        assert isinstance(error, dict)
        assert error["code"] == "PLUGIN_CALL_FAILED"
        assert "scipy.linalg" in str(error.get("message", ""))
        assert not setup_marker.exists()
        assert not real_linalg.exists()
        assert process.poll() is None
        _support._close(process, plugin_id)
    finally:
        _support._reap(process)


@pytest.mark.skipif(os.name != "nt", reason="private SciPy preload is Windows-only")
def test_scipy_rejects_replaced_private_numpy_origin(tmp_path: Path) -> None:
    plugin_id = "fixture.scipy.numpy-origin"
    plugin_root = tmp_path / "plugin"
    dependency_root = tmp_path / "deps"
    setup_marker = tmp_path / "setup.txt"
    _support._numpy(dependency_root, "ORIGIN = 'dependency-numpy'\n")
    _scipy(
        dependency_root,
        """
import sys
import types
ORIGIN = "dependency-scipy"
fake = types.ModuleType("numpy")
fake.__file__ = r"C:\\global\\numpy\\__init__.py"
fake.ORIGIN = "global-numpy"
sys.modules["numpy"] = fake
""",
        "ORIGIN = 'dependency-linalg'\n",
    )
    _support._plugin(
        plugin_root,
        f"""
from pathlib import Path

class Service:
    def info(self):
        return {{"ran": True}}

class Plugin:
    def setup(self, context):
        Path({str(setup_marker)!r}).write_text("ran", encoding="utf-8")
        context.provide("fixture.scipy", Service(), exports=("info",))
""",
    )
    process = _support._popen(
        _support._command(plugin_root, tmp_path / "data", plugin_id, dependency_root, validate=False),
        tmp_path,
    )
    try:
        frame = _support._request(process, plugin_id, "runtime.initialize", {})
        assert frame["ok"] is False
        error = frame["error"]
        assert isinstance(error, dict)
        assert error["code"] == "PLUGIN_CALL_FAILED"
        assert "numpy" in str(error.get("message", ""))
        assert not setup_marker.exists()
        assert process.poll() is None
        _support._close(process, plugin_id)
    finally:
        _support._reap(process)
