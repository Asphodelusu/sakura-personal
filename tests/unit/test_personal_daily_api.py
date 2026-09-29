from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from tools.personal_daily_api import ABSENT, DailyApiError, prepare_api


def test_cli_is_directly_executable() -> None:
    repository = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(repository / "tools/personal_daily_api.py"), "--help"],
        cwd=repository, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert "--use-source-api" in result.stdout


def _api() -> dict:
    profiles = [
        {"id": f"p{i}", "alias": f"Provider {i}", "base_url": f"https://p{i}.invalid/v1",
         "api_key": f"synthetic-{i}", "models": [
             {"name": f"m{i}-{j}", "context_window_tokens": 8192 + j}
             for j in range(5 if i < 4 else 4)]}
        for i in range(5)
    ]
    slots = {name: {"profile_id": f"p{i}", "model": f"m{i}-0"}
             for i, name in enumerate(("chat", "vision_chat", "inner_thought", "chat_fast", "memory_curation"))}
    return {"config_version": 1, "api_profiles": profiles, "model_slots": slots,
            "tts": {"provider": "synthetic"}}


def _write(path: Path, value: dict) -> str:
    data = yaml.safe_dump(value, sort_keys=False).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def test_existing_current_is_authoritative_and_byte_exact(tmp_path: Path) -> None:
    source, current, staging = (tmp_path / name for name in ("old/api.yaml", "new/api.yaml", "stage"))
    source_hash = _write(source, _api())
    current_value = _api()
    current_value["api_profiles"][0]["alias"] = "Current owner"
    current_hash = _write(current, current_value)
    before = current.read_bytes()
    result = prepare_api(source, current, staging, source_hash, current_hash)
    assert result == {"selected": "current", "providers": 5, "models": 24, "slots": 5}
    assert (staging / "config/api.yaml").read_bytes() == before
    assert source.read_bytes() != before
    assert current.read_bytes() == before


def test_explicit_source_carries_all_models_slots_and_removes_tts(tmp_path: Path) -> None:
    source, current, staging = (tmp_path / name for name in ("old/api.yaml", "new/api.yaml", "stage"))
    source_hash = _write(source, _api())
    current_hash = _write(current, {"api_profiles": [], "model_slots": {}})
    result = prepare_api(source, current, staging, source_hash, current_hash, use_source_api=True)
    migrated = yaml.safe_load((staging / "config/api.yaml").read_text(encoding="utf-8"))
    assert result == {"selected": "source", "providers": 5, "models": 24, "slots": 5}
    assert migrated["api_profiles"] == _api()["api_profiles"]
    assert migrated["model_slots"] == _api()["model_slots"]
    assert "tts" not in migrated
    assert "tts" in yaml.safe_load(source.read_text(encoding="utf-8"))


@pytest.mark.parametrize("fixture", ["missing_alias", "malformed_yaml"])
def test_explicit_source_replaces_invalid_current_without_touching_it(tmp_path: Path, fixture: str) -> None:
    source, current, staging = (tmp_path / name for name in ("old/api.yaml", "new/api.yaml", "stage"))
    source_hash = _write(source, _api())
    if fixture == "missing_alias":
        current_value = _api()
        current_value["api_profiles"][0].pop("alias")
        current_hash = _write(current, current_value)
    else:
        current.parent.mkdir(parents=True)
        current.write_bytes(b"api_profiles: [\n  api_key: SYNTHETIC_FIXTURE\n")
        current_hash = hashlib.sha256(current.read_bytes()).hexdigest()
    before = current.read_bytes()
    result = prepare_api(source, current, staging, source_hash, current_hash, use_source_api=True)
    assert result == {"selected": "source", "providers": 5, "models": 24, "slots": 5}
    assert current.read_bytes() == before
    assert yaml.safe_load((staging / "config/api.yaml").read_text(encoding="utf-8"))["model_slots"] == _api()["model_slots"]


def test_default_rejects_invalid_current_without_staging(tmp_path: Path) -> None:
    source, current, staging = (tmp_path / name for name in ("old/api.yaml", "new/api.yaml", "stage"))
    source_hash = _write(source, _api())
    current_value = _api()
    current_value["api_profiles"][0].pop("alias")
    current_hash = _write(current, current_value)
    with pytest.raises(DailyApiError, match="API_INVALID"):
        prepare_api(source, current, staging, source_hash, current_hash)
    assert not staging.exists()


def test_absent_current_uses_source_and_rejects_changed_current(tmp_path: Path) -> None:
    source, current, staging = (tmp_path / name for name in ("old/api.yaml", "new/api.yaml", "stage"))
    source_hash = _write(source, _api())
    current.parent.mkdir(parents=True)
    assert prepare_api(source, current, staging, source_hash, ABSENT)["selected"] == "source"
    current_hash = _write(current, _api())
    with pytest.raises(DailyApiError, match="CURRENT_HASH_MISMATCH"):
        prepare_api(source, current, tmp_path / "second", source_hash, ABSENT)
    assert current_hash
    assert not (tmp_path / "second").exists()


@pytest.mark.parametrize("change,code", [("source", "SOURCE_HASH_MISMATCH"), ("current", "CURRENT_HASH_MISMATCH")])
def test_stale_hash_fails_without_staging(tmp_path: Path, change: str, code: str) -> None:
    source, current, staging = (tmp_path / name for name in ("old/api.yaml", "new/api.yaml", "stage"))
    source_hash = _write(source, _api())
    current_hash = _write(current, _api())
    (source if change == "source" else current).write_bytes(b"changed")
    with pytest.raises(DailyApiError, match=code):
        prepare_api(source, current, staging, source_hash, current_hash)
    assert not staging.exists()


@pytest.mark.parametrize("mutation", [
    lambda value: value["api_profiles"].pop(),
    lambda value: value["api_profiles"][0]["models"].pop(0),
    lambda value: value["model_slots"].update(chat={"profile_id": "p0", "model": "missing"}),
])
def test_invalid_source_contract_fails_without_staging(tmp_path: Path, mutation) -> None:
    source, current, staging = (tmp_path / name for name in ("old/api.yaml", "new/api.yaml", "stage"))
    value = _api()
    mutation(value)
    source_hash = _write(source, value)
    current.parent.mkdir(parents=True)
    with pytest.raises(DailyApiError, match="API_INVALID"):
        prepare_api(source, current, staging, source_hash, ABSENT)
    assert not staging.exists()


def test_smaller_valid_configuration_is_preserved(tmp_path: Path) -> None:
    source, current, staging = (tmp_path / name for name in ("old/api.yaml", "new/api.yaml", "stage"))
    value = _api()
    value["api_profiles"] = value["api_profiles"][:1]
    value["model_slots"] = {"chat": value["model_slots"]["chat"]}
    source_hash = _write(source, value)
    current.parent.mkdir(parents=True)
    result = prepare_api(source, current, staging, source_hash, ABSENT)
    assert result == {"selected": "source", "providers": 1, "models": 5, "slots": 1}


def test_current_change_during_preparation_aborts_before_staging(tmp_path: Path, monkeypatch) -> None:
    import tools.personal_daily_api as daily_api

    source, current, staging = (tmp_path / name for name in ("old/api.yaml", "new/api.yaml", "stage"))
    source_hash = _write(source, _api())
    current_hash = _write(current, _api())
    original = daily_api._snapshot
    changed = False

    def changing_snapshot(path, expected, code, *, allow_absent):
        nonlocal changed
        data = original(path, expected, code, allow_absent=allow_absent)
        if path == current and not changed:
            changed = True
            current.write_bytes(b"changed while preparing")
        return data

    monkeypatch.setattr(daily_api, "_snapshot", changing_snapshot)
    with pytest.raises(DailyApiError, match="CURRENT_HASH_MISMATCH"):
        prepare_api(source, current, staging, source_hash, current_hash)
    assert not staging.exists()


def test_source_change_during_preparation_aborts_before_staging(tmp_path: Path, monkeypatch) -> None:
    import tools.personal_daily_api as daily_api

    source, current, staging = (tmp_path / name for name in ("old/api.yaml", "new/api.yaml", "stage"))
    source_hash = _write(source, _api())
    current.parent.mkdir(parents=True)
    original = daily_api._snapshot
    changed = False

    def changing_snapshot(path, expected, code, *, allow_absent):
        nonlocal changed
        data = original(path, expected, code, allow_absent=allow_absent)
        if path == source and not changed:
            changed = True
            source.write_bytes(b"changed while preparing")
        return data

    monkeypatch.setattr(daily_api, "_snapshot", changing_snapshot)
    with pytest.raises(DailyApiError, match="SOURCE_HASH_MISMATCH"):
        prepare_api(source, current, staging, source_hash, ABSENT)
    assert not staging.exists()


def test_equivalent_dotdot_staging_path_is_rejected(tmp_path: Path) -> None:
    source, current = (tmp_path / name for name in ("old/api.yaml", "new/api.yaml"))
    source_hash = _write(source, _api())
    current.parent.mkdir(parents=True)
    (tmp_path / "else").mkdir()
    staging = tmp_path / "else" / ".." / "old" / "stage"
    with pytest.raises(DailyApiError, match="PATH_UNSAFE"):
        prepare_api(source, current, staging, source_hash, ABSENT)
    assert not (tmp_path / "old/stage").exists()


def test_cli_error_does_not_echo_credentials(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    source, current, staging = (tmp_path / name for name in ("old/api.yaml", "new/api.yaml", "stage"))
    source.parent.mkdir(parents=True)
    source.write_text("api_profiles: [\n  api_key: PRIVATE_SYNTHETIC_SECRET\n", encoding="utf-8")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    current.parent.mkdir(parents=True)
    result = subprocess.run(
        [sys.executable, str(repository / "tools/personal_daily_api.py"),
         "--source-api", str(source), "--current-api", str(current), "--staging", str(staging),
         "--expected-source-sha256", source_hash, "--expected-current-sha256", ABSENT],
        cwd=repository, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert result.stderr.strip() == "API_INVALID"
    assert "PRIVATE_SYNTHETIC_SECRET" not in result.stdout + result.stderr
    assert not staging.exists()


def test_existing_staging_and_linked_source_are_rejected(tmp_path: Path) -> None:
    source, current, staging = (tmp_path / name for name in ("old/api.yaml", "new/api.yaml", "stage"))
    source_hash = _write(source, _api())
    current.parent.mkdir(parents=True)
    staging.mkdir()
    with pytest.raises(DailyApiError, match="STAGING_EXISTS"):
        prepare_api(source, current, staging, source_hash, ABSENT)
    staging.rmdir()
    linked = tmp_path / "linked.yaml"
    try:
        linked.symlink_to(source)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(DailyApiError, match="PATH_UNSAFE"):
        prepare_api(linked, current, staging, source_hash, ABSENT)
    assert not staging.exists()
