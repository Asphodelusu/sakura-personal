"""Inner-thought settings, hidden model slots, and canonical migration."""

from __future__ import annotations

from pathlib import Path

import yaml

from app.agent.inner_thought import InnerThoughtSettings
from app.agent.runtime import AgentRuntime
from app.config.model_slots import resolve_inner_thought_model
from app.config.models import (
    ApiConfigProfile,
    ModelSelectionSettings,
    ModelSlotSelection,
)
from app.config.provider_model_settings import ProviderModelSettingsRepository
from app.core_host.inner_thought_settings import (
    attach_inner_thought,
    load_inner_thought_settings,
)
from app.legacy_import.configuration import migrate_configuration
from app.llm.api_client import ApiSettings


def test_settings_follow_source_defaults_and_canonical_file() -> None:
    defaults = InnerThoughtSettings().normalized()
    assert defaults.enabled is True
    assert defaults.window_size == 6
    assert defaults.timeout_seconds == 8
    assert defaults.join_timeout_seconds == 3
    assert defaults.skip_fast_tier is True
    assert defaults.skip_proactive is True
    assert InnerThoughtSettings(window_size=99, timeout_seconds=0).normalized().window_size == 16
    assert InnerThoughtSettings(timeout_seconds=0).normalized().timeout_seconds == 1


def test_loader_prefers_canonical_inner_thought_settings(tmp_path: Path) -> None:
    assert load_inner_thought_settings(tmp_path).enabled is True
    canonical = tmp_path / "config" / "system_config.yaml"
    legacy = tmp_path / "data" / "config" / "system_config.yaml"
    canonical.parent.mkdir(parents=True)
    legacy.parent.mkdir(parents=True)
    canonical.write_text("inner_thought:\n  enabled: false\n  window_size: 4\n", encoding="utf-8")
    legacy.write_text("inner_thought:\n  enabled: true\n  window_size: 9\n", encoding="utf-8")
    loaded = load_inner_thought_settings(tmp_path)
    assert loaded.enabled is False
    assert loaded.window_size == 4


def test_partial_selection_is_invalid_instead_of_inheriting():
    from app.core_host.inner_thought_settings import _selections
    for value in ({"profile_id": "fixture"}, {"model": "chosen"}):
        _selection, invalid = _selections({"inner_thought": value})
        assert "inner_thought" in invalid


def test_explicit_invalid_selection_disables_without_fallback() -> None:
    profiles = [
        ApiConfigProfile(
            id="fixture",
            alias="Fixture",
            base_url="https://fixture.invalid/v1",
            api_key="secret",
            models=("chat-model", "fast-model"),
        )
    ]
    base = ApiSettings(base_url="https://fixture.invalid/v1", api_key="secret", model="chat-model", timeout_seconds=60)
    invalid = resolve_inner_thought_model(
        profiles,
        ModelSelectionSettings(
            chat=ModelSlotSelection("fixture", "chat-model"),
            inner_thought=ModelSlotSelection("fixture", "missing-model"),
        ),
        base,
    )
    assert invalid.settings is None
    assert invalid.source_slot == "inner_thought"
    assert invalid.reason == "INNER_THOUGHT_SELECTION_INVALID"

    invalid_fast = resolve_inner_thought_model(
        profiles,
        ModelSelectionSettings(
            chat=ModelSlotSelection("fixture", "chat-model"),
            chat_fast=ModelSlotSelection("missing-profile", "fast-model"),
        ),
        base,
    )
    assert invalid_fast.settings is None
    assert invalid_fast.source_slot == "chat_fast"

    fast = resolve_inner_thought_model(
        profiles,
        ModelSelectionSettings(
            chat=ModelSlotSelection("fixture", "chat-model"),
            chat_fast=ModelSlotSelection("fixture", "fast-model"),
        ),
        base,
    )
    assert fast.source_slot == "chat_fast"
    assert fast.settings is not None
    assert fast.settings.model == "fast-model"
    assert base.model == "chat-model"

    chat = resolve_inner_thought_model(
        profiles,
        ModelSelectionSettings(chat=ModelSlotSelection("fixture", "chat-model")),
        base,
    )
    assert chat.source_slot == "chat"
    assert chat.settings is not None
    assert chat.settings.model == "chat-model"


def test_attach_uses_dedicated_client_and_invalid_selection_makes_zero_calls(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write_api(
        root,
        inner={"profile_id": "fixture", "model": "missing-model"},
        fast={"profile_id": "fixture", "model": "fast-model"},
    )
    runtime = AgentRuntime(object(), "system", character_id="sakura", character_name="Sakura")
    main_client = runtime.api_client
    attach_inner_thought(runtime, root)
    assert runtime.api_client is main_client
    calls: list[object] = []
    runtime._inner_thought._client = _CountingClient(calls)  # type: ignore[attr-defined]
    # Invalid configured selection must ignore the planted counting client.
    attach_inner_thought(runtime, root)
    assert runtime.start_inner_thought("op", [{"role": "user", "content": "hi"}]) is False
    assert calls == []

    _write_api(root, fast={"profile_id": "fixture", "model": "fast-model"})
    attach_inner_thought(runtime, root)
    choice_client = runtime._inner_thought._client  # type: ignore[attr-defined]
    assert choice_client is not None
    assert choice_client.settings.model == "fast-model"
    assert choice_client.settings.timeout_seconds <= 8
    assert choice_client._request_attempts == 1
    assert runtime.api_client is main_client


def test_provider_save_keeps_hidden_slots_out_of_public_snapshot(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write_api(
        root,
        inner={"profile_id": "fixture", "model": "thought-model"},
        fast={"profile_id": "fixture", "model": "fast-model"},
    )
    ProviderModelSettingsRepository(root).save(_draft())
    saved = yaml.safe_load((root / "config" / "api.yaml").read_text(encoding="utf-8"))
    assert saved["model_slots"]["inner_thought"]["model"] == "thought-model"
    assert saved["model_slots"]["chat_fast"]["model"] == "fast-model"
    snapshot = ProviderModelSettingsRepository(root).snapshot()
    assert set(snapshot["model_slots"]) == {"chat", "vision_chat"}


def test_migration_copies_inner_thought_without_overwriting_canonical(tmp_path: Path) -> None:
    source = tmp_path / "source"
    legacy = source / "data" / "config"
    legacy.mkdir(parents=True)
    (legacy / "system_config.yaml").write_text(
        yaml.safe_dump({"inner_thought": {"enabled": True, "window_size": 6, "legacy_only": 1}}),
        encoding="utf-8",
    )
    (legacy / "api.yaml").write_text(
        yaml.safe_dump(
            {
                "api_profiles": [],
                "model_slots": {
                    "chat": {"profile_id": "legacy", "model": "old-chat"},
                    "inner_thought": {"profile_id": "legacy", "model": "old-thought"},
                    "chat_fast": {"profile_id": "legacy", "model": "old-fast"},
                },
            }
        ),
        encoding="utf-8",
    )
    existing = tmp_path / "existing"
    (existing / "config").mkdir(parents=True)
    (existing / "config" / "system_config.yaml").write_text(
        "inner_thought:\n  enabled: false\n  window_size: 2\n",
        encoding="utf-8",
    )
    (existing / "config" / "api.yaml").write_text(
        yaml.safe_dump(
            {
                "model_slots": {
                    "inner_thought": {"profile_id": "canonical", "model": "kept-thought"},
                    "chat_fast": {"profile_id": "canonical", "model": "kept-fast"},
                }
            }
        ),
        encoding="utf-8",
    )
    staged = tmp_path / "staged"
    migrate_configuration(source, staged, new_tts_root=tmp_path / "tts", existing_user_root=existing)
    written = yaml.safe_load((staged / "config" / "system_config.yaml").read_text(encoding="utf-8"))
    assert written["inner_thought"] == {"enabled": False, "window_size": 2}
    slots = yaml.safe_load((staged / "config" / "api.yaml").read_text(encoding="utf-8"))["model_slots"]
    assert slots["inner_thought"]["model"] == "kept-thought"
    assert slots["chat_fast"]["model"] == "kept-fast"
    assert slots["chat"]["model"] == "old-chat"

    fresh = tmp_path / "fresh-source"
    fresh_config = fresh / "data" / "config"
    fresh_config.mkdir(parents=True)
    (fresh_config / "system_config.yaml").write_text(
        yaml.safe_dump({"inner_thought": {"enabled": False, "window_size": 5}}),
        encoding="utf-8",
    )
    staged_fresh = tmp_path / "staged-fresh"
    migrate_configuration(fresh, staged_fresh, new_tts_root=tmp_path / "tts2")
    copied = yaml.safe_load((staged_fresh / "config" / "system_config.yaml").read_text(encoding="utf-8"))
    assert copied["inner_thought"]["enabled"] is False
    assert copied["inner_thought"]["window_size"] == 5


def _root(tmp_path: Path) -> Path:
    config = tmp_path / "config"
    config.mkdir(parents=True)
    (config / "system_config.yaml").write_text(
        yaml.safe_dump({"config_version": 1, "inner_thought": {"enabled": True}}),
        encoding="utf-8",
    )
    return tmp_path


def _write_api(root: Path, *, inner: dict[str, str] | None = None, fast: dict[str, str] | None = None) -> None:
    slots: dict[str, dict[str, str]] = {
        "chat": {"profile_id": "fixture", "model": "chat-model"},
        "vision_chat": {"profile_id": "fixture", "model": "vision-model"},
    }
    if inner is not None:
        slots["inner_thought"] = inner
    if fast is not None:
        slots["chat_fast"] = fast
    (root / "config" / "api.yaml").write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "base_url": "https://fixture.invalid/v1",
                    "api_key": "secret",
                    "model": "chat-model",
                    "timeout_seconds": 60,
                },
                "api_profiles": [
                    {
                        "id": "fixture",
                        "alias": "Fixture",
                        "base_url": "https://fixture.invalid/v1",
                        "api_key": "secret",
                        "models": [
                            {"name": "chat-model"},
                            {"name": "fast-model"},
                            {"name": "thought-model"},
                            {"name": "vision-model"},
                        ],
                    }
                ],
                "model_slots": slots,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _draft() -> dict[str, object]:
    return {
        "providers": [
            {
                "id": "fixture",
                "alias": "Fixture",
                "base_url": "https://fixture.invalid/v1",
                "models": ["chat-model", "fast-model", "thought-model", "vision-model"],
                "credential": {"action": "keep", "value": ""},
            }
        ],
        "model_slots": {
            "chat": {"profile_id": "fixture", "model": "chat-model"},
            "vision_chat": {"profile_id": "fixture", "model": "vision-model"},
        },
        "settings": {"timeout_seconds": 30},
    }


class _CountingClient:
    def __init__(self, calls: list[object]) -> None:
        self.calls = calls

    def complete_raw(self, *_args: object, **_kwargs: object) -> str:
        self.calls.append(object())
        return "interest: low\n静か。"
