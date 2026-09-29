from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from tools.release import stage_distribution


def _source(root: Path) -> Path:
    source = root / "personal"
    for name in ("numpy", "torch", "sentence_transformers"):
        package = source / name
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
    return source


def _manifest(entry: str) -> str:
    return (
        "api: 4\n"
        "id: sakura.memory.mem0\n"
        "name: Mem0 长期记忆\n"
        "version: 0.1.0\n"
        f"entry: {entry}\n"
        "enabled: true\n"
        "priority: 100\n"
    )


def _repo(tmp_path: Path, *, entry: str = "plugin:SakuraMem0Plugin") -> Path:
    repo = tmp_path / "repo"
    mem0 = repo / "plugins/builtin/sakura_mem0"
    mem0.mkdir(parents=True)
    (mem0 / "plugin.yaml").write_text(_manifest(entry), encoding="utf-8")
    web = repo / "plugins/builtin/sakura_web"
    web.mkdir(parents=True)
    (web / "plugin.yaml").write_text(
        "id: sakura.web\nversion: 9.9.9\nentry: plugin:WebPlugin\n",
        encoding="utf-8",
    )
    (repo / "VERSION").write_text("1.0.0-personal.1\n", encoding="utf-8")
    layout = repo / "desktop/src-tauri/runtime-layouts/windows-x64"
    layout.mkdir(parents=True)
    (layout / "runtime-manifest.json").write_text("{}\n", encoding="utf-8")
    return repo


def _light_assemble(monkeypatch: pytest.MonkeyPatch, output: Path) -> list[tuple[str, str]]:
    observed: list[tuple[str, str]] = []

    def fake_copy(source: Path, target: Path, *, extra_ignored: tuple[str, ...] = ()) -> None:
        if source.name == "builtin":
            shutil.copytree(source, target)
            return
        target.mkdir(parents=True, exist_ok=True)

    def watch(name: str):
        def _record(*_args: object, **_kwargs: object) -> dict[str, object]:
            manifest = output / "plugins/builtin/sakura_mem0/plugin.yaml"
            observed.append((name, manifest.read_text(encoding="utf-8")))
            return {"schemaVersion": 2, "version": "1.0.0-personal.1", "files": []}

        return _record

    monkeypatch.setattr(stage_distribution, "copy_tree", fake_copy)
    monkeypatch.setattr(stage_distribution, "move_tools", lambda *_a, **_k: None)
    monkeypatch.setattr(stage_distribution, "stage_bundled_dependencies", lambda *_a, **_k: None)
    monkeypatch.setattr(stage_distribution, "prune_non_runtime_files", lambda *_a, **_k: None)
    monkeypatch.setattr(stage_distribution, "write_windows_pth", lambda *_a, **_k: None)
    monkeypatch.setattr(stage_distribution, "validate_layout", watch("validate"))
    monkeypatch.setattr(stage_distribution, "smoke_personal_dependencies", watch("smoke"))
    monkeypatch.setattr(stage_distribution, "inventory", watch("inventory"))
    monkeypatch.setattr(stage_distribution, "_python_version", lambda _path: "3.12")
    monkeypatch.setattr(
        "tools.release.diagnostic_build.write_mapping", lambda *_a, **_k: None
    )
    return observed


def test_private_dependencies_without_daily_keep_default_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    source_manifest = (repo / "plugins/builtin/sakura_mem0/plugin.yaml").read_text(encoding="utf-8")
    output = tmp_path / "staging"
    observed = _light_assemble(monkeypatch, output)
    stage_distribution.assemble(
        repo,
        tmp_path / "python",
        output,
        "windows-x64",
        portable=False,
        personal_dependencies=_source(tmp_path),
    )
    staged = (output / "plugins/builtin/sakura_mem0/plugin.yaml").read_text(encoding="utf-8")
    assert staged == source_manifest
    assert "plugin:PersonalDailyPlugin" not in staged
    assert (repo / "plugins/builtin/sakura_mem0/plugin.yaml").read_text(encoding="utf-8") == source_manifest
    assert [name for name, _text in observed] == ["validate", "smoke", "inventory", "inventory"]
    assert all("entry: plugin:SakuraMem0Plugin\n" in text for _name, text in observed)


def test_personal_daily_switches_only_staged_entry_before_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    source_path = repo / "plugins/builtin/sakura_mem0/plugin.yaml"
    source_manifest = source_path.read_text(encoding="utf-8")
    web_manifest = (repo / "plugins/builtin/sakura_web/plugin.yaml").read_text(encoding="utf-8")
    output = tmp_path / "staging"
    observed = _light_assemble(monkeypatch, output)
    stage_distribution.assemble(
        repo,
        tmp_path / "python",
        output,
        "windows-x64",
        portable=False,
        personal_dependencies=_source(tmp_path),
        personal_daily=True,
    )
    staged = (output / "plugins/builtin/sakura_mem0/plugin.yaml").read_text(encoding="utf-8")
    assert staged == source_manifest.replace(
        "entry: plugin:SakuraMem0Plugin\n",
        "entry: plugin:PersonalDailyPlugin\n",
        1,
    )
    assert "id: sakura.memory.mem0\n" in staged
    assert "version: 0.1.0\n" in staged
    assert source_path.read_text(encoding="utf-8") == source_manifest
    assert (output / "plugins/builtin/sakura_web/plugin.yaml").read_text(encoding="utf-8") == web_manifest
    assert (repo / "plugins/builtin/sakura_web/plugin.yaml").read_text(encoding="utf-8") == web_manifest
    assert [name for name, _text in observed] == ["validate", "smoke", "inventory", "inventory"]
    assert all("entry: plugin:PersonalDailyPlugin\n" in text for _name, text in observed)


@pytest.mark.parametrize(
    ("target", "with_dependencies", "code"),
    [
        ("windows-x64", False, "PERSONAL_DAILY_DEPENDENCIES_REQUIRED"),
        ("linux-x64", True, "PERSONAL_DAILY_WINDOWS_ONLY"),
    ],
)
def test_personal_daily_rejects_invalid_combination_before_output(
    tmp_path: Path, target: str, with_dependencies: bool, code: str
) -> None:
    repo = _repo(tmp_path)
    output = tmp_path / "staging"
    dependencies = _source(tmp_path) if with_dependencies else None
    with pytest.raises(ValueError, match=code):
        stage_distribution.assemble(
            repo,
            tmp_path / "python",
            output,
            target,
            portable=False,
            personal_dependencies=dependencies,
            personal_daily=True,
        )
    assert not output.exists()


def test_personal_daily_rejects_unexpected_source_entry_before_output(tmp_path: Path) -> None:
    repo = _repo(tmp_path, entry="plugin:OtherPlugin")
    source_path = repo / "plugins/builtin/sakura_mem0/plugin.yaml"
    original = source_path.read_text(encoding="utf-8")
    output = tmp_path / "staging"
    with pytest.raises(ValueError, match="PERSONAL_DAILY_ENTRY_UNEXPECTED"):
        stage_distribution.assemble(
            repo,
            tmp_path / "python",
            output,
            "windows-x64",
            portable=False,
            personal_dependencies=_source(tmp_path),
            personal_daily=True,
        )
    assert not output.exists()
    assert source_path.read_text(encoding="utf-8") == original


def test_cli_forwards_personal_daily_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[bool] = []
    output = tmp_path / "out"
    python_root = tmp_path / "python"
    python_root.mkdir()
    dependencies = tmp_path / "deps"
    dependencies.mkdir()

    def fake_assemble(*_args: object, **kwargs: object) -> None:
        recorded.append(bool(kwargs["personal_daily"]))
        output.mkdir(parents=True, exist_ok=True)
        (output / "release-inventory.json").write_text(
            json.dumps(
                {
                    "target": "windows-x64",
                    "version": "1.0.0-personal.1",
                    "fileCount": 0,
                    "uncompressedBytes": 0,
                    "topLevelBytes": {},
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(stage_distribution, "assemble", fake_assemble)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stage_distribution.py",
            "--target",
            "windows-x64",
            "--python-root",
            str(python_root),
            "--output",
            str(output),
            "--personal-mem0-dependencies",
            str(dependencies),
            "--personal-daily",
        ],
    )
    assert stage_distribution.main() == 0
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stage_distribution.py",
            "--target",
            "windows-x64",
            "--python-root",
            str(python_root),
            "--output",
            str(output),
            "--personal-mem0-dependencies",
            str(dependencies),
        ],
    )
    assert stage_distribution.main() == 0
    assert recorded == [True, False]


def test_windows_package_forwards_personal_daily_and_guards_before_output() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts/package_windows.ps1").read_text(
        encoding="utf-8"
    )
    assert "[switch]$PersonalDaily" in script
    updater_guard = script.index("-PersonalDaily 不能与 -Updater 或 -UpdaterArtifacts 同时使用。")
    version_guard = script.index("-PersonalDaily 需要带连字符的预发布 VERSION。")
    dependency_guard = script.index("-PersonalDaily 需要显式的 -PersonalMem0Dependencies。")
    output_creation = script.index("New-Item -ItemType Directory -Path $cacheRoot")
    cleanup = script.index("Remove-BuildDirectory $buildRoot")
    assert max(updater_guard, version_guard, dependency_guard) < output_creation
    assert max(updater_guard, version_guard, dependency_guard) < cleanup
    assert script.index("if ($PersonalDaily)") < script.index('+= "--personal-daily"')
    for stage_output in ('"--output", $releaseStage', '"--output", $portableStage'):
        call = script.index(stage_output)
        assert "$personalMem0Arguments" in script[call : call + 180]
