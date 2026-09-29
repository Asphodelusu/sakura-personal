#!/usr/bin/env python3
"""Assemble and validate the only distribution staging consumed by Tauri."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app/plugin_sdk"))
from sakura_downloads import uv_download_environment

TARGETS = {"windows-x64", "macos-arm64", "linux-x64"}
BUILTIN_PLUGINS = {
    "sakura_portrait",
    "sakura_web",
    "sakura_mem0",
    "sakura_mobile",
    "sakura_tts_hub",
    "sakura_asr_hub",
    "sakura_asr_sensevoice",
    "sakura_genie",
    "sakura_gpt_sovits",
}
BUNDLED_DEPENDENCY_DIRECTORIES = {
    "sakura_web",
    "sakura_asr_sensevoice",
    "sakura_mem0",
    "sakura_genie",
    "sakura_gpt_sovits",
}
CORE_IMPORTS = (
    "yaml",
    "py7zz",
    "mcp",
)
PLUGIN_ONLY_IMPORTS = (
    "sherpa_onnx",
    "sherpa_onnx_core",
    "playwright",
    "openai",
    "qdrant_client",
    "sqlalchemy",
    "posthog",
    "pytz",
    "google",
    "fastembed",
    "onnxruntime",
    "py7zr",
    "socksio",
)
FORBIDDEN_PARTS = {
    ".cache",
    ".local-browsers",
    "fastembed-cache",
    "hf-cache",
    "ms-playwright",
    "gpt-sovits",
    "gpt_sovits-v2",
    "all-minilm-l6-v2-onnx",
    "genie-runtime",
}
FORBIDDEN_SUFFIXES = {".ckpt", ".pth", ".safetensors"}
IGNORED_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".DS_Store"}
DEPENDENCY_TEST_DIRECTORIES = {"test", "tests"}
PERSONAL_IMPORTS = ("numpy", "torch", "sentence_transformers")


def copy_tree(source: Path, target: Path, *, extra_ignored: tuple[str, ...] = ()) -> None:
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
            "*.pyo",
            ".DS_Store",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            *sorted(FORBIDDEN_PARTS),
            *extra_ignored,
        ),
    )


def python_executable(python_root: Path, target: str) -> Path:
    return python_root / ("python.exe" if target == "windows-x64" else "bin/python3")


def site_packages(python_root: Path, target: str) -> Path:
    if target == "windows-x64":
        return python_root / "Lib/site-packages"
    return python_root / "lib/python3.12/site-packages"


def move_tools(python_root: Path, target: str) -> None:
    tools = python_root / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    executable_suffix = ".exe" if target == "windows-x64" else ""
    search_roots = [python_root / "Scripts", python_root / "bin", python_root]
    names = [
        (f"uv{executable_suffix}", [root / f"uv{executable_suffix}" for root in search_roots]),
        (f"uvx{executable_suffix}", [root / f"uvx{executable_suffix}" for root in search_roots]),
        (
            f"7zz{executable_suffix}",
            [site_packages(python_root, target) / "py7zz" / "bin" / f"7zz{executable_suffix}"],
        ),
    ]
    for name, candidates in names:
        source = next((candidate for candidate in candidates if candidate.is_file()), None)
        if source is None:
            raise ValueError(f"STAGING_TOOL_MISSING: {name}")
        target_path = tools / name
        shutil.copy2(source, target_path)
        target_path.chmod(target_path.stat().st_mode | stat.S_IXUSR)
        if source != target_path:
            source.unlink()


def write_windows_pth(python_root: Path) -> None:
    pth = python_root / "python312._pth"
    pth.write_text("python312.zip\n.\nLib/site-packages\nimport site\n", encoding="utf-8", newline="\n")


def _python_version(executable: Path) -> str:
    result = subprocess.run(
        [
            str(executable),
            "-I",
            "-S",
            "-c",
            "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
    )
    return result.stdout.strip()


def _is_reparse_point(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError as error:
        raise ValueError("PERSONAL_DEPENDENCIES_PATH_UNSAFE") from error
    return path.is_symlink() or bool(
        getattr(details, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def validate_personal_dependency_source(source: Path, output: Path, target: str) -> Path:
    if target != "windows-x64":
        raise ValueError("PERSONAL_DEPENDENCIES_WINDOWS_ONLY")
    source = Path(source).absolute()
    output = Path(output).absolute()
    if not source.is_dir():
        raise ValueError("PERSONAL_DEPENDENCIES_INCOMPLETE")
    for part in (source, *source.parents):
        if _is_reparse_point(part):
            raise ValueError("PERSONAL_DEPENDENCIES_PATH_UNSAFE")
    resolved_source = source.resolve()
    resolved_output = output.resolve()
    if (
        resolved_source == resolved_output
        or resolved_source in resolved_output.parents
        or resolved_output in resolved_source.parents
    ):
        raise ValueError("PERSONAL_DEPENDENCIES_PATH_UNSAFE")
    for root, dirs, files in os.walk(source, followlinks=False):
        for name in (*dirs, *files):
            if _is_reparse_point(Path(root) / name):
                raise ValueError("PERSONAL_DEPENDENCIES_PATH_UNSAFE")
    if any(not (source / name / "__init__.py").is_file() for name in PERSONAL_IMPORTS):
        raise ValueError("PERSONAL_DEPENDENCIES_INCOMPLETE")
    marker_path = source / ".sakura-dependencies.json"
    if marker_path.exists():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("PERSONAL_DEPENDENCIES_MARKER_INVALID") from error
        if (
            not isinstance(marker, dict)
            or marker.get("schemaVersion") != 1
            or marker.get("kind") != "requirements.txt"
            or marker.get("python") != "3.12"
        ):
            raise ValueError("PERSONAL_DEPENDENCIES_MARKER_INVALID")
    return resolved_source


def stage_bundled_dependencies(
    stage: Path, target: str, *, personal_dependencies: Path | None = None
) -> None:
    executable = python_executable(stage / "python", target)
    suffix = ".exe" if target == "windows-x64" else ""
    uv = stage / "python/tools" / f"uv{suffix}"
    python_version = _python_version(executable)
    if personal_dependencies is not None and python_version != "3.12":
        raise ValueError("PERSONAL_DEPENDENCIES_PYTHON_INVALID")
    dependency_parent = stage / "plugins/dependencies"
    dependency_parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONNOUSERSITE": "1",
            "UV_PYTHON_DOWNLOADS": "never",
        }
    )
    for directory_name in sorted(BUNDLED_DEPENDENCY_DIRECTORIES):
        plugin_root = stage / "plugins/builtin" / directory_name
        plugin_id = _manifest_plugin_id(plugin_root / "plugin.yaml")
        requirements = plugin_root / "requirements.txt"
        dependency_root = dependency_parent / plugin_id
        if directory_name == "sakura_mem0" and personal_dependencies is not None:
            copy_tree(
                personal_dependencies,
                dependency_root,
                extra_ignored=("*.whl", ".lock"),
            )
        else:
            dependency_root.mkdir()
            subprocess.run(
                [
                    str(uv),
                    "pip",
                    "install",
                    "--target",
                    str(dependency_root),
                    "--python",
                    str(executable),
                    "--no-python-downloads",
                    "--link-mode",
                    "clone" if target == "macos-arm64" else "hardlink",
                    "--no-progress",
                    "--requirements",
                    str(requirements),
                ],
                check=True,
                cwd=plugin_root,
                env=uv_download_environment(plugin_root, environment),
                timeout=600,
            )
        marker = {
            "schemaVersion": 1,
            "kind": "requirements.txt",
            "python": python_version,
        }
        (dependency_root / ".sakura-dependencies.json").write_text(
            json.dumps(marker, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )


def verify_personal_imports(
    executable: Path, dependency_root: Path, plugin_root: Path, plugin_sdk_root: Path
) -> None:
    """Prove private imports with a fresh isolated interpreter, without loading models."""
    script = (
        "import importlib, sys\n"
        "from pathlib import Path\n"
        f"root = Path({str(dependency_root)!r}).resolve()\n"
        f"roots = [{str(plugin_sdk_root)!r}, {str(plugin_root)!r}, str(root)]\n"
        "stdlib = [item for item in sys.path if item and 'site-packages' not in item "
        "and 'dist-packages' not in item]\n"
        "sys.path[:] = list(dict.fromkeys([*roots, *stdlib]))\n"
        f"for name in {PERSONAL_IMPORTS!r}:\n"
        "    module = importlib.import_module(name)\n"
        "    origin = Path(module.__file__).resolve()\n"
        "    if not origin.is_relative_to(root):\n"
        "        raise ImportError(f'{name} outside private root: {origin}')\n"
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment.update({
        "PYTHONNOUSERSITE": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "ANONYMIZED_TELEMETRY": "False",
        "HF_HUB_DISABLE_TELEMETRY": "1",
    })
    subprocess.run(
        [str(executable), "-I", "-B", "-S", "-c", script],
        check=True,
        cwd=plugin_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )


def smoke_personal_dependencies(stage: Path, target: str) -> None:
    if target != "windows-x64":
        raise ValueError("PERSONAL_DEPENDENCIES_WINDOWS_ONLY")
    verify_personal_imports(
        python_executable(stage / "python", target),
        stage / "plugins/dependencies/sakura.memory.mem0",
        stage / "plugins/builtin/sakura_mem0",
        stage / "core/app/plugin_sdk",
    )


def _manifest_value(path: Path, key: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}:\s*(.+?)\s*$")
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.fullmatch(line)
        if match is not None:
            return match.group(1).strip('"\'')
    raise ValueError(f"STAGING_PLUGIN_MANIFEST_INVALID: {path.parent.name}:{key}")


def _manifest_plugin_id(path: Path) -> str:
    plugin_id = _manifest_value(path, "id")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", plugin_id):
        raise ValueError(f"STAGING_PLUGIN_MANIFEST_INVALID: {path.parent.name}:id")
    return plugin_id


def smoke_bundled_entries(stage: Path, target: str) -> None:
    executable = python_executable(stage / "python", target)
    runner = stage / "core/app/plugins/plugin_runner_v4.py"
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["PYTHONNOUSERSITE"] = "1"
    with tempfile.TemporaryDirectory(prefix="sakura-release-plugin-smoke-") as data_root:
        for directory_name in sorted(BUILTIN_PLUGINS):
            plugin_root = stage / "plugins/builtin" / directory_name
            plugin_id = _manifest_plugin_id(plugin_root / "plugin.yaml")
            command = [
                str(executable),
                "-I",
                "-B",
                "-S",
                str(runner),
                "--plugin-id",
                plugin_id,
                "--generation-id",
                "release-smoke",
                "--plugin-root",
                str(plugin_root),
                "--data-dir",
                str(Path(data_root) / plugin_id),
                "--entry",
                _manifest_value(plugin_root / "plugin.yaml", "entry"),
                "--validate-entry",
            ]
            dependency_root = stage / "plugins/dependencies" / plugin_id
            if dependency_root.is_dir():
                command.extend(["--dependency-root", str(dependency_root)])
            subprocess.run(
                command,
                check=True,
                cwd=plugin_root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )


def forbidden_paths(stage: Path) -> list[str]:
    failures: list[str] = []
    for path in stage.rglob("*"):
        relative = path.relative_to(stage)
        lowered = {part.lower() for part in relative.parts}
        suffix = path.suffix.lower()
        python_path_file = path.is_file() and suffix == ".pth" and (
            path.parent.name.lower() == "site-packages"
            or (
                len(relative.parts) == 4
                and tuple(part.lower() for part in relative.parts[:2])
                == ("plugins", "dependencies")
            )
        )
        if lowered & FORBIDDEN_PARTS or (
            path.is_file()
            and suffix in FORBIDDEN_SUFFIXES
            and not python_path_file
        ):
            failures.append(relative.as_posix())
    return failures


def prune_non_runtime_files(stage: Path, target: str) -> None:
    """Remove installer artifacts that imports and plugin execution never consume."""
    roots = [site_packages(stage / "python", target)]
    dependency_parent = stage / "plugins/dependencies"
    if dependency_parent.is_dir():
        roots.extend(path for path in dependency_parent.iterdir() if path.is_dir())
    for root in roots:
        if not root.is_dir():
            continue
        test_directories = sorted(
            (
                path
                for path in root.rglob("*")
                if path.is_dir() and path.name.lower() in DEPENDENCY_TEST_DIRECTORIES
            ),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for directory in test_directories:
            shutil.rmtree(directory)
        for cache in sorted(root.rglob("__pycache__"), key=lambda path: len(path.parts), reverse=True):
            if cache.is_dir():
                shutil.rmtree(cache)
        for compiled in (*root.rglob("*.pyc"), *root.rglob("*.pyo")):
            compiled.unlink()
    if target == "windows-x64":
        # Console entry points created by pip are build-machine launchers. Core
        # calls Python modules directly and keeps the three supported tools in
        # python/tools instead.
        shutil.rmtree(stage / "python/Scripts", ignore_errors=True)
    if dependency_parent.is_dir():
        for dependency_root in dependency_parent.iterdir():
            if dependency_root.is_dir():
                # Plugin runners import dependency modules; none executes pip's
                # generated console entry points. Nested package binaries such
                # as py7zz/bin/7zz remain untouched.
                shutil.rmtree(dependency_root / "bin", ignore_errors=True)


def validate_layout(stage: Path, target: str, *, portable: bool) -> None:
    required = [
        stage / "VERSION",
        stage / "runtime-manifest.json",
        python_executable(stage / "python", target),
        site_packages(stage / "python", target),
        stage / "core/app/core_host/__main__.py",
        stage / "core/app/legacy_import/__main__.py",
        stage / "plugins/builtin/__init__.py",
        stage / "python/tools" / ("uv.exe" if target == "windows-x64" else "uv"),
        stage / "python/tools" / ("uvx.exe" if target == "windows-x64" else "uvx"),
        stage / "python/tools" / ("7zz.exe" if target == "windows-x64" else "7zz"),
    ]
    missing = [path.relative_to(stage).as_posix() for path in required if not path.exists()]
    if missing:
        raise ValueError(f"STAGING_LAYOUT_INCOMPLETE: {', '.join(missing)}")
    actual_plugins = {
        path.parent.name for path in (stage / "plugins/builtin").glob("*/plugin.yaml")
    }
    if actual_plugins != BUILTIN_PLUGINS:
        raise ValueError(
            f"STAGING_PLUGIN_SET_INVALID: expected={sorted(BUILTIN_PLUGINS)!r}, actual={sorted(actual_plugins)!r}"
        )
    for plugin_id in sorted(BUILTIN_PLUGINS):
        manifest = stage / "plugins/builtin" / plugin_id / "plugin.yaml"
        if _manifest_value(manifest, "api") != "4":
            raise ValueError(f"STAGING_PLUGIN_API_INVALID: {plugin_id}")
    if (stage / "plugins/optional").exists():
        raise ValueError("STAGING_CONTAINS_OPTIONAL_PLUGINS")
    dependency_roots = stage / "plugins/dependencies"
    actual_dependency_roots = (
        {path.name for path in dependency_roots.iterdir() if path.is_dir()}
        if dependency_roots.is_dir()
        else set()
    )
    expected_dependency_roots = {
        _manifest_plugin_id(stage / "plugins/builtin" / directory_name / "plugin.yaml")
        for directory_name in BUNDLED_DEPENDENCY_DIRECTORIES
    }
    if actual_dependency_roots != expected_dependency_roots:
        raise ValueError(
            "STAGING_PLUGIN_DEPENDENCIES_INVALID: "
            f"expected={sorted(expected_dependency_roots)!r}, "
            f"actual={sorted(actual_dependency_roots)!r}"
        )
    for directory_name in sorted(BUNDLED_DEPENDENCY_DIRECTORIES):
        plugin_root = stage / "plugins/builtin" / directory_name
        plugin_id = _manifest_plugin_id(plugin_root / "plugin.yaml")
        requirements = plugin_root / "requirements.txt"
        marker_path = dependency_roots / plugin_id / ".sakura-dependencies.json"
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError(f"STAGING_PLUGIN_DEPENDENCIES_INVALID: {plugin_id}")
        if (
            not requirements.is_file()
            or not isinstance(marker, dict)
            or marker.get("schemaVersion") != 1
            or marker.get("kind") != "requirements.txt"
            or marker.get("python") != "3.12"
        ):
            raise ValueError(f"STAGING_PLUGIN_DEPENDENCIES_INVALID: {plugin_id}")
    for user_owned in ("config", "data", "characters", "tts"):
        if (stage / user_owned).exists():
            raise ValueError(f"STAGING_CONTAINS_USER_DATA: {user_owned}")
    if (stage / "plugins/user").exists():
        raise ValueError("STAGING_CONTAINS_USER_DATA: plugins/user")
    if (stage / "core/third_party").exists():
        raise ValueError("STAGING_CONTAINS_LEGACY_THIRD_PARTY")
    if (stage / "portable.flag").exists() != portable:
        raise ValueError("STAGING_PORTABLE_FLAG_INVALID")
    if not any(site_packages(stage / "python", target).glob("*.dist-info")):
        raise ValueError("STAGING_DIST_INFO_MISSING")
    forbidden = forbidden_paths(stage)
    if forbidden:
        raise ValueError(f"STAGING_FORBIDDEN_CONTENT: {', '.join(forbidden[:20])}")


def smoke(stage: Path, target: str) -> None:
    executable = python_executable(stage / "python", target)
    modules = ",".join(repr(name) for name in CORE_IMPORTS)
    plugin_only = ",".join(repr(name) for name in PLUGIN_ONLY_IMPORTS)
    script = (
        "import importlib,importlib.util,sys;"
        f"sys.path[:0]=[{str(stage / 'core')!r},{str(stage)!r}];"
        f"[importlib.import_module(name) for name in [{modules}]];"
        f"assert all(importlib.util.find_spec(name) is None for name in [{plugin_only}]);"
        "import app.core_host,app.legacy_import,plugins.builtin"
    )
    subprocess.run([str(executable), "-I", "-B", "-c", script], check=True, timeout=90)
    smoke_bundled_entries(stage, target)
    suffix = ".exe" if target == "windows-x64" else ""
    for name, argument in (("uv", "--version"), ("uvx", "--version"), ("7zz", "-h")):
        subprocess.run(
            [str(stage / "python/tools" / f"{name}{suffix}"), argument],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )


def inventory(stage: Path, target: str) -> dict[str, object]:
    files = []
    directory_sizes: dict[str, int] = {}
    total = 0
    for path in sorted(item for item in stage.rglob("*") if item.is_file()):
        relative = path.relative_to(stage).as_posix()
        if relative == "release-inventory.json":
            continue
        size = path.stat().st_size
        total += size
        top = relative.partition("/")[0]
        directory_sizes[top] = directory_sizes.get(top, 0) + size
        files.append({"path": relative, "size": size})
    return {
        "schemaVersion": 2,
        "target": target,
        "version": (stage / "VERSION").read_text(encoding="utf-8").strip(),
        "fileCount": len(files),
        "uncompressedBytes": total,
        "topLevelBytes": dict(sorted(directory_sizes.items())),
        "files": files,
    }


def assemble(
    repo: Path, python_source: Path, output: Path, target: str, *, portable: bool,
    personal_dependencies: Path | None = None,
) -> None:
    if personal_dependencies is not None:
        personal_dependencies = validate_personal_dependency_source(
            personal_dependencies, output, target
        )
        if _python_version(python_executable(python_source, target)) != "3.12":
            raise ValueError("PERSONAL_DEPENDENCIES_PYTHON_INVALID")
    if output.exists() and any(output.iterdir()):
        raise ValueError("STAGING_OUTPUT_NOT_EMPTY")
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(repo / "VERSION", output / "VERSION")
    shutil.copy2(
        repo / f"desktop/src-tauri/runtime-layouts/{target}/runtime-manifest.json",
        output / "runtime-manifest.json",
    )
    copy_tree(python_source, output / "python")
    copy_tree(repo / "app", output / "core/app")
    (output / "plugins").mkdir(exist_ok=True)
    copy_tree(repo / "plugins/builtin", output / "plugins/builtin")
    move_tools(output / "python", target)
    stage_bundled_dependencies(output, target, personal_dependencies=personal_dependencies)
    prune_non_runtime_files(output, target)
    if target == "windows-x64":
        write_windows_pth(output / "python")
    if portable:
        (output / "portable.flag").write_bytes(b"")
    validate_layout(output, target, portable=portable)
    if personal_dependencies is not None:
        smoke_personal_dependencies(output, target)
    try:
        from .diagnostic_build import write_mapping
    except ImportError:
        from diagnostic_build import write_mapping
    write_mapping(repo, output, target, inventory(output, target))
    (output / "release-inventory.json").write_text(
        json.dumps(inventory(output, target), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, choices=sorted(TARGETS))
    parser.add_argument("--python-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--portable", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--personal-mem0-dependencies", type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    python_root = args.python_root.resolve()
    output = args.output.resolve()
    if output == repo or repo in output.parents and output.name in {"data", "characters", "plugins"}:
        raise SystemExit("STAGING_OUTPUT_UNSAFE")
    assemble(
        repo, python_root, output, args.target, portable=args.portable,
        personal_dependencies=args.personal_mem0_dependencies,
    )
    if args.smoke:
        smoke(output, args.target)
    report = json.loads((output / "release-inventory.json").read_text(encoding="utf-8"))
    print(json.dumps({key: report[key] for key in ("target", "version", "fileCount", "uncompressedBytes", "topLevelBytes")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
