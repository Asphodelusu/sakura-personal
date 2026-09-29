"""Staged migration of legacy core_maintainer into plugin coreMaintainer."""
import json
from pathlib import Path

import pytest
import yaml

from app.legacy_import.configuration import migrate_configuration
from app.legacy_import.errors import LegacyImportError


def _source(tmp_path: Path, system: dict) -> tuple[Path, Path]:
    source, staged = tmp_path / "source", tmp_path / "staged"
    config = source / "data" / "config"
    config.mkdir(parents=True)
    (config / "system_config.yaml").write_text(
        yaml.safe_dump(system, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return source, staged


def _plugin_config(staged: Path) -> dict:
    return json.loads(
        (staged / "data/plugins/sakura.memory.mem0/config.json").read_text(encoding="utf-8")
    )


def test_missing_legacy_maintainer_encodes_old_defaults_only_in_staging(tmp_path: Path) -> None:
    source, staged = _source(tmp_path, {"tool_loop": {"max_agent_steps_per_turn": 3}})
    migrate_configuration(source, staged, new_tts_root=tmp_path / "tts")
    saved = _plugin_config(staged)
    maintainer = saved["coreMaintainer"]
    assert maintainer["enabled"] is True
    assert maintainer["observed_min_evidence"] == 3
    assert maintainer["observed_min_batches"] == 2
    assert maintainer["observed_min_span_minutes"] == 30
    assert maintainer["observed_min_confidence"] == 0.8
    assert maintainer["normal_cooldown_hours"] == 6
    assert maintainer["stale_eligible_hours"] == 72
    assert maintainer["max_candidates_per_call"] == 5
    assert maintainer["max_sections_per_call"] == 2
    assert maintainer["pause_after_validation_failures"] == 3
    assert maintainer["pause_hours"] == 24
    assert maintainer["lease_ttl_minutes"] == 30
    assert not (source / "data/plugins/sakura.memory.mem0/config.json").exists()


def test_legacy_maintainer_mapping_is_normalized_without_dropping_other_keys(tmp_path: Path) -> None:
    source, staged = _source(
        tmp_path,
        {"memory": {"core_maintainer": {"enabled": False, "normal_cooldown_hours": 9, "extra": "ignore"}}},
    )
    destination = staged / "data/plugins/sakura.memory.mem0/config.json"
    destination.parent.mkdir(parents=True)
    destination.write_text(json.dumps({"curationModel": "keep-me"}) + "\n", encoding="utf-8")
    migrate_configuration(source, staged, new_tts_root=tmp_path / "tts")
    saved = _plugin_config(staged)
    assert saved["curationModel"] == "keep-me"
    assert saved["coreMaintainer"]["enabled"] is False
    assert saved["coreMaintainer"]["normal_cooldown_hours"] == 9
    assert "extra" not in saved["coreMaintainer"]


def test_existing_staged_core_maintainer_stays_authoritative(tmp_path: Path) -> None:
    source, staged = _source(tmp_path, {"memory": {"core_maintainer": {"enabled": True}}})
    destination = staged / "data/plugins/sakura.memory.mem0/config.json"
    destination.parent.mkdir(parents=True)
    destination.write_text(
        json.dumps({"curationModel": "keep-me", "coreMaintainer": {"enabled": False, "pause_hours": 2}}) + "\n",
        encoding="utf-8",
    )
    before = destination.read_bytes()
    migrate_configuration(source, staged, new_tts_root=tmp_path / "tts")
    assert destination.read_bytes() == before


def test_invalid_legacy_maintainer_is_refused(tmp_path: Path) -> None:
    source, staged = _source(tmp_path, {"memory": {"core_maintainer": ["reset-me"]}})
    destination = staged / "data/plugins/sakura.memory.mem0/config.json"
    destination.parent.mkdir(parents=True)
    destination.write_text(json.dumps({"curationModel": "keep-me"}) + "\n", encoding="utf-8")
    before = destination.read_bytes()
    with pytest.raises(LegacyImportError) as caught:
        migrate_configuration(source, staged, new_tts_root=tmp_path / "tts")
    assert caught.value.code == "LEGACY_CORE_MAINTAINER_CONFIG_INVALID"
    assert destination.read_bytes() == before


def test_existing_user_root_core_maintainer_is_preserved_in_new_staging(tmp_path):
    source, staged = _source(tmp_path, {"memory": {"core_maintainer": {"enabled": True}}})
    existing = tmp_path / "existing"
    path = existing / "data/plugins/sakura.memory.mem0/config.json"
    path.parent.mkdir(parents=True)
    settings = {"curationModel": "target-model", "coreMaintainer": {"enabled": False, "pause_hours": 2}}
    path.write_text(json.dumps(settings), encoding="utf-8")
    before = path.read_bytes()
    migrate_configuration(source, staged, new_tts_root=tmp_path / "tts", existing_user_root=existing)
    assert _plugin_config(staged) == settings
    assert path.read_bytes() == before
