"""Prepare an API file in a new staging directory for a later, separately guarded publish.

This never edits either input or the current user root. Publishing must stop
writers or hold a lock and recheck the current file hash at its commit boundary.
TTS and coreMaintainer have separate ownership decisions.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import urlsplit

import yaml

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.legacy_import.configuration import _normalize_api


ABSENT = "absent"
_DIGEST = re.compile(r"[0-9a-fA-F]{64}\Z")


class DailyApiError(ValueError):
    """A fixed, credential-free preparation error code."""


def _plain_path(path: Path) -> None:
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise DailyApiError("PATH_UNSAFE")


def _snapshot(path: Path, expected: str, code: str, *, allow_absent: bool) -> bytes | None:
    _plain_path(path)
    if expected == ABSENT:
        if not allow_absent or os.path.lexists(path):
            raise DailyApiError(code)
        return None
    if not _DIGEST.fullmatch(expected):
        raise DailyApiError("HASH_INVALID")
    try:
        data = path.read_bytes()
    except (OSError, ValueError) as exc:
        raise DailyApiError(code) from None
    if hashlib.sha256(data).hexdigest() != expected.lower():
        raise DailyApiError(code)
    return data


def _parse_api(data: bytes) -> dict:
    try:
        value = yaml.safe_load(data.decode("utf-8"))
    except (UnicodeError, yaml.YAMLError):
        raise DailyApiError("API_INVALID") from None
    if not isinstance(value, dict):
        raise DailyApiError("API_INVALID")
    return value


def _validate_api(value: dict) -> dict[str, int]:
    profiles = value.get("api_profiles")
    slots = value.get("model_slots")
    if not isinstance(profiles, list) or not isinstance(slots, dict):
        raise DailyApiError("API_INVALID")
    models_by_profile: dict[str, set[str]] = {}
    model_count = 0
    for profile in profiles:
        if not isinstance(profile, dict):
            raise DailyApiError("API_INVALID")
        profile_id = profile.get("id")
        if not isinstance(profile_id, str) or not profile_id.strip() or profile_id in models_by_profile:
            raise DailyApiError("API_INVALID")
        if not isinstance(profile.get("alias"), str) or not profile["alias"].strip():
            raise DailyApiError("API_INVALID")
        base_url = profile.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            raise DailyApiError("API_INVALID")
        try:
            endpoint = urlsplit(base_url)
            hostname = endpoint.hostname
            endpoint.port
        except ValueError:
            raise DailyApiError("API_INVALID") from None
        if endpoint.scheme not in {"http", "https"} or not hostname:
            raise DailyApiError("API_INVALID")
        if not isinstance(profile.get("api_key"), str):
            raise DailyApiError("API_INVALID")
        models = profile.get("models")
        if not isinstance(models, list):
            raise DailyApiError("API_INVALID")
        names: set[str] = set()
        for model in models:
            if not isinstance(model, dict) or not isinstance(model.get("name"), str):
                raise DailyApiError("API_INVALID")
            name = model["name"]
            if not name.strip() or name in names:
                raise DailyApiError("API_INVALID")
            names.add(name)
        models_by_profile[profile_id] = names
        model_count += len(names)
    for slot in slots.values():
        if not isinstance(slot, dict):
            raise DailyApiError("API_INVALID")
        profile_id, model = slot.get("profile_id", ""), slot.get("model", "")
        if not isinstance(profile_id, str) or not isinstance(model, str):
            raise DailyApiError("API_INVALID")
        if bool(profile_id.strip()) != bool(model.strip()):
            raise DailyApiError("API_INVALID")
        if profile_id and model not in models_by_profile.get(profile_id, set()):
            raise DailyApiError("API_INVALID")
    return {"providers": len(profiles), "models": model_count, "slots": len(slots)}


def prepare_api(
    source_api: Path, current_api: Path, staging: Path,
    expected_source_sha256: str, expected_current_sha256: str,
    *, use_source_api: bool = False,
) -> dict[str, str | int]:
    """Validate explicit snapshots, then create only ``staging/config/api.yaml``."""
    source_api, current_api, staging = (Path(item).absolute() for item in (source_api, current_api, staging))
    for path in (source_api, current_api, staging):
        _plain_path(path)
    source_api, current_api, staging = (path.resolve(strict=False) for path in (source_api, current_api, staging))
    if (source_api == current_api or staging == source_api or staging == current_api
            or staging.is_relative_to(source_api.parent)
            or staging.is_relative_to(current_api.parent)
            or source_api.is_relative_to(staging) or current_api.is_relative_to(staging)):
        raise DailyApiError("PATH_UNSAFE")
    if os.path.lexists(staging):
        raise DailyApiError("STAGING_EXISTS")
    if not staging.parent.is_dir():
        raise DailyApiError("PATH_UNSAFE")
    source_data = _snapshot(source_api, expected_source_sha256, "SOURCE_HASH_MISMATCH", allow_absent=False)
    current_data = _snapshot(current_api, expected_current_sha256, "CURRENT_HASH_MISMATCH", allow_absent=True)
    assert source_data is not None
    source = _parse_api(source_data)
    normalized = deepcopy(source)
    try:
        repairs = _normalize_api(normalized)
    except Exception:
        raise DailyApiError("API_INVALID") from None
    if (repairs or normalized.get("api_profiles") != source.get("api_profiles")
            or normalized.get("model_slots") != source.get("model_slots")):
        raise DailyApiError("API_REPAIR_REQUIRED")
    _validate_api(normalized)
    selected = "source" if use_source_api or current_data is None else "current"
    if selected == "current":
        output = current_data
        assert output is not None
        counts = _validate_api(_parse_api(output))
    else:
        normalized.pop("tts", None)
        counts = _validate_api(normalized)
        output = yaml.safe_dump(normalized, allow_unicode=True, sort_keys=False).encode("utf-8")
    if (_snapshot(source_api, expected_source_sha256, "SOURCE_HASH_MISMATCH", allow_absent=False) != source_data
            or _snapshot(current_api, expected_current_sha256, "CURRENT_HASH_MISMATCH", allow_absent=True) != current_data):
        raise DailyApiError("INPUT_CHANGED")
    _plain_path(staging)
    if os.path.lexists(staging):
        raise DailyApiError("STAGING_EXISTS")
    try:
        staging.mkdir()
    except FileExistsError:
        raise DailyApiError("STAGING_EXISTS") from None
    except OSError:
        raise DailyApiError("STAGING_WRITE_FAILED") from None
    config = staging / "config"
    try:
        config.mkdir()
        with (config / "api.yaml").open("xb") as stream:
            stream.write(output)
    except Exception:
        (config / "api.yaml").unlink(missing_ok=True)
        if config.exists():
            config.rmdir()
        staging.rmdir()
        raise
    return {"selected": selected, **counts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-api", required=True, type=Path)
    parser.add_argument("--current-api", required=True, type=Path)
    parser.add_argument("--staging", required=True, type=Path)
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument("--expected-current-sha256", required=True,
                        help="SHA-256 of current API, or 'absent' if it does not exist")
    parser.add_argument("--use-source-api", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = prepare_api(args.source_api, args.current_api, args.staging,
                             args.expected_source_sha256, args.expected_current_sha256,
                             use_source_api=args.use_source_api)
    except DailyApiError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, yaml.YAMLError):
        print("PREPARATION_FAILED", file=sys.stderr)
        return 2
    print(f"{result['selected']} providers={result['providers']} models={result['models']} slots={result['slots']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
