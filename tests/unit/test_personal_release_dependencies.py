from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from tools.release import stage_distribution


def _source(root: Path, *, marker: dict[str, object] | None = None) -> Path:
    source = root / "personal"
    for name in ("numpy", "torch", "sentence_transformers"):
        package = source / name
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
    if marker is not None:
        (source / ".sakura-dependencies.json").write_text(json.dumps(marker), encoding="utf-8")
    return source


def test_personal_source_without_marker_is_accepted_before_output_creation(tmp_path: Path) -> None:
    source = _source(tmp_path)
    output = tmp_path / "staging"
    assert stage_distribution.validate_personal_dependency_source(source, output, "windows-x64") == source
    assert not output.exists()


@pytest.mark.parametrize("target", ["macos-arm64", "linux-x64"])
def test_personal_source_rejects_other_targets_without_output(tmp_path: Path, target: str) -> None:
    source = _source(tmp_path)
    output = tmp_path / "staging"
    with pytest.raises(ValueError, match="PERSONAL_DEPENDENCIES_WINDOWS_ONLY"):
        stage_distribution.validate_personal_dependency_source(source, output, target)
    assert not output.exists()


def test_personal_source_rejects_missing_package_and_wrong_marker(tmp_path: Path) -> None:
    source = _source(tmp_path, marker={"schemaVersion": 1, "kind": "requirements.txt", "python": "3.11"})
    output = tmp_path / "staging"
    with pytest.raises(ValueError, match="PERSONAL_DEPENDENCIES_MARKER_INVALID"):
        stage_distribution.validate_personal_dependency_source(source, output, "windows-x64")
    (source / ".sakura-dependencies.json").unlink()
    (source / "torch/__init__.py").unlink()
    with pytest.raises(ValueError, match="PERSONAL_DEPENDENCIES_INCOMPLETE"):
        stage_distribution.validate_personal_dependency_source(source, output, "windows-x64")
    assert not output.exists()


@pytest.mark.parametrize("within_source", [True, False])
def test_personal_source_rejects_source_output_containment(tmp_path: Path, within_source: bool) -> None:
    source = _source(tmp_path)
    output = source / "staging" if within_source else tmp_path
    with pytest.raises(ValueError, match="PERSONAL_DEPENDENCIES_PATH_UNSAFE"):
        stage_distribution.validate_personal_dependency_source(source, output, "windows-x64")


def test_personal_source_rejects_reparse_entry(tmp_path: Path) -> None:
    source = _source(tmp_path)
    external = tmp_path / "external.txt"
    external.write_text("private", encoding="utf-8")
    try:
        (source / "external-link").symlink_to(external)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ValueError, match="PERSONAL_DEPENDENCIES_PATH_UNSAFE"):
        stage_distribution.validate_personal_dependency_source(source, tmp_path / "staging", "windows-x64")


def test_personal_stage_skips_mem0_uv_and_writes_standard_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source(tmp_path)
    stage = tmp_path / "stage"
    for name in stage_distribution.BUNDLED_DEPENDENCY_DIRECTORIES:
        plugin = stage / "plugins/builtin" / name
        plugin.mkdir(parents=True)
        (plugin / "plugin.yaml").write_text(f"id: sakura.{name}\n", encoding="utf-8")
        (plugin / "requirements.txt").write_text("fixture\n", encoding="utf-8")
    (stage / "python/tools").mkdir(parents=True)
    (stage / "python/tools/uv.exe").write_bytes(b"")
    (stage / "python/python.exe").write_bytes(b"")
    monkeypatch.setattr(stage_distribution, "_python_version", lambda _: "3.12")
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(stage_distribution.subprocess, "run", fake_run)
    stage_distribution.stage_bundled_dependencies(stage, "windows-x64", personal_dependencies=source)
    assert len(commands) == len(stage_distribution.BUNDLED_DEPENDENCY_DIRECTORIES) - 1
    assert all("sakura_mem0" not in " ".join(command) for command in commands)
    destination = stage / "plugins/dependencies/sakura.sakura_mem0"
    assert (destination / "numpy/__init__.py").is_file()
    assert json.loads((destination / ".sakura-dependencies.json").read_text()) == {
        "schemaVersion": 1, "kind": "requirements.txt", "python": "3.12"
    }


def test_wrong_bundled_python_rejected_before_output_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    python_root = tmp_path / "python"
    python_root.mkdir()
    monkeypatch.setattr(stage_distribution, "_python_version", lambda _: "3.11")
    output = tmp_path / "staging"
    with pytest.raises(ValueError, match="PERSONAL_DEPENDENCIES_PYTHON_INVALID"):
        stage_distribution.assemble(
            tmp_path, python_root, output, "windows-x64",
            portable=False, personal_dependencies=source,
        )
    assert not output.exists()


def test_private_import_probe_uses_isolated_python_and_rejects_external_origin(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    plugin = tmp_path / "plugin"
    sdk = tmp_path / "sdk"
    plugin.mkdir()
    sdk.mkdir()
    stage_distribution.verify_personal_imports(Path(sys.executable), source, plugin, sdk)
    (source / "torch/__init__.py").unlink()
    (sdk / "torch").mkdir()
    (sdk / "torch/__init__.py").write_text("", encoding="utf-8")
    with pytest.raises(subprocess.CalledProcessError):
        stage_distribution.verify_personal_imports(Path(sys.executable), source, plugin, sdk)


def test_windows_package_checks_personal_source_before_any_stage_cleanup() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts/package_windows.ps1").read_text(
        encoding="utf-8"
    )
    preflight = script.index("validate_personal_dependency_source")
    cleanup = script.index("Remove-BuildDirectory $buildRoot")
    assert preflight < cleanup
    for protected in ("$buildRoot", "$releaseStage", "$portableStage"):
        assert protected in script[preflight:cleanup]
