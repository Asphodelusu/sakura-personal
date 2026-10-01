"""Personal proactive values drive the observer; upstream minutes must not mask them."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.agent.runtime import AgentRuntime
from app.config.relationship_initiative import RelationshipInitiativeSettings
from app.core_host.screen_awareness_settings import ScreenAwarenessSettingsBoundary


GENERATION_ID = "00000000-0000-4000-8000-000000004407"
GENERATION_CREDENTIAL = "b" * 32
SNAPSHOT = {
    "hwnd": 7,
    "pid": 7,
    "process": "editor.exe",
    "title": "notes",
    "ownProcess": False,
}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _request(name: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "id": name,
        "name": name,
        "generationId": GENERATION_ID,
        "generationCredential": GENERATION_CREDENTIAL,
        "protocolMajor": 2,
        "protocolMinor": 2,
        "payload": payload,
    }


def test_personal_proactive_timer_overrides_upstream_minutes(tmp_path: Path) -> None:
    path = tmp_path / "config" / "system_config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "config_version": 1,
                "screen_awareness": {
                    "enabled": True,
                    "check_interval_minutes": 20,
                    "cooldown_minutes": 10,
                },
                "proactive": {
                    "enabled": True,
                    "timer_seconds": 40,
                    "cooldown_seconds": 600,
                    "min_silence_after_user": 10,
                },
            }
        ),
        encoding="utf-8",
    )
    clock = Clock()
    clock.now = 100
    runtime = AgentRuntime(object(), "SYNTHETIC_PERSONA", character_id="fixture")
    runtime.configure_personal_reply(personal_style=True)
    runtime.configure_initiative(
        RelationshipInitiativeSettings(proactive_enabled=False),
        client=None,
        clock=clock,
        idle_seconds=lambda: 0,
    )
    runtime.configure_screen_initiative(enabled=True, cooldown_seconds=600)
    boundary = ScreenAwarenessSettingsBoundary(
        GENERATION_ID,
        GENERATION_CREDENTIAL,
        tmp_path,
        session_provider=lambda: SimpleNamespace(runtime=runtime),
    )

    def advance() -> dict[str, object]:
        response = boundary.handle(_request(
            "screen_awareness.focus.advance",
            {"busy": False, "scope": GENERATION_ID, "snapshot": SNAPSHOT},
        ))
        assert response["ok"] is True
        return response["payload"]

    assert advance()["reason"] == "silence"
    clock.now = 130
    assert advance()["trigger"] == ""
    clock.now = 180
    assert advance()["trigger"] == "timer"
    shown = boundary.handle(_request("screen_awareness.settings.get", {}))
    assert shown["payload"]["settings"]["timerSeconds"] == 40
    assert "checkIntervalMinutes" not in shown["payload"]["settings"]


def _personal_package(root: Path) -> None:
    package = root / "characters" / "fixture"
    package.mkdir(parents=True)
    (package / "card.md").write_text("card\n", encoding="utf-8")
    (package / "system_guards.md").write_text("guards\n", encoding="utf-8")
    (package / "character.json").write_text(
        '{"id":"fixture","display_name":"Fixture","card":"card.md","system_guards":"system_guards.md"}\n',
        encoding="utf-8",
    )
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "characters.yaml").write_text("current_character_id: fixture\n", encoding="utf-8")


def _runtime(clock: Clock, *, personal: bool, idle_seconds=lambda: 0) -> AgentRuntime:
    runtime = AgentRuntime(object(), "SYNTHETIC_PERSONA", character_id="fixture")
    runtime.configure_personal_reply(personal_style=personal)
    runtime.configure_initiative(
        RelationshipInitiativeSettings(proactive_enabled=False),
        client=None,
        clock=clock,
        idle_seconds=idle_seconds,
    )
    runtime.configure_screen_initiative(enabled=True, cooldown_seconds=600)
    return runtime


def _boundary(tmp_path: Path, runtime: AgentRuntime) -> ScreenAwarenessSettingsBoundary:
    return ScreenAwarenessSettingsBoundary(
        GENERATION_ID,
        GENERATION_CREDENTIAL,
        tmp_path,
        session_provider=lambda: SimpleNamespace(runtime=runtime),
    )


def _advance(boundary: ScreenAwarenessSettingsBoundary, snapshot: dict[str, object]) -> dict[str, object]:
    response = boundary.handle(_request(
        "screen_awareness.focus.advance",
        {"busy": False, "scope": GENERATION_ID, "snapshot": snapshot},
    ))
    assert response["ok"] is True
    return response["payload"]


def test_personal_save_preserves_unexposed_fields_and_rejects_invalid_bytes(tmp_path: Path) -> None:
    path = tmp_path / "config" / "system_config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "config_version: 1\n"
        "keep_top: yes\n"
        "screen_awareness:\n"
        "  check_interval_minutes: 20\n"
        "proactive:\n"
        "  timer_seconds: 40\n"
        "  content_check_interval: 45\n"
        "  custom_note: keep\n"
        "  game_ocr_enabled: true\n",
        encoding="utf-8",
    )
    clock = Clock()
    boundary = _boundary(tmp_path, _runtime(clock, personal=True))
    current = boundary.handle(_request("screen_awareness.settings.get", {}))["payload"]["settings"]
    assert current["pollIntervalSeconds"] == 5
    saved = boundary.handle(_request(
        "screen_awareness.settings.save",
        {"settings": {**current, "timerSeconds": 90.5, "focusSettleDelay": 4}},
    ))
    assert saved["ok"] is True
    assert saved["payload"]["settings"]["timerSeconds"] == 90.5
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert document["keep_top"] is True
    assert document["screen_awareness"]["check_interval_minutes"] == 20
    assert document["proactive"]["custom_note"] == "keep"
    assert document["proactive"]["content_check_interval"] == 45
    assert document["proactive"]["game_ocr_enabled"] is False
    assert document["proactive"]["timer_seconds"] == 90.5

    before = path.read_bytes()
    rejected = boundary.handle(_request(
        "screen_awareness.settings.save",
        {"settings": {**current, "timerSeconds": 0}},
    ))
    assert rejected["error"]["code"] == "FIELD_INVALID"
    assert path.read_bytes() == before


def test_canonical_proactive_file_wins_and_legacy_is_only_a_fallback(tmp_path: Path) -> None:
    legacy = tmp_path / "data" / "config" / "system_config.yaml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("proactive:\n  timer_seconds: 40\n", encoding="utf-8")
    clock = Clock()
    boundary = _boundary(tmp_path, _runtime(clock, personal=True))
    assert boundary.handle(_request("screen_awareness.settings.get", {}))["payload"]["settings"]["timerSeconds"] == 40

    canonical = tmp_path / "config" / "system_config.yaml"
    canonical.parent.mkdir(parents=True)
    canonical.write_text(
        "config_version: 1\nproactive:\n  timer_seconds: 90\n",
        encoding="utf-8",
    )
    assert boundary.handle(_request("screen_awareness.settings.get", {}))["payload"]["settings"]["timerSeconds"] == 90


def test_missing_proactive_switch_falls_back_without_adopting_upstream_minutes(tmp_path: Path) -> None:
    path = tmp_path / "config" / "system_config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "config_version: 1\n"
        "screen_awareness:\n"
        "  enabled: false\n"
        "  check_interval_minutes: 2\n"
        "proactive:\n"
        "  timer_seconds: nan\n"
        "  poll_interval: 2.5\n",
        encoding="utf-8",
    )
    boundary = _boundary(tmp_path, _runtime(Clock(), personal=True))
    settings = boundary.handle(_request("screen_awareness.settings.get", {}))["payload"]["settings"]
    assert settings["enabled"] is False
    assert settings["timerSeconds"] == 480
    assert settings["pollIntervalSeconds"] == 2.5


def test_selected_personal_card_is_personal_before_a_session_exists(tmp_path: Path) -> None:
    _personal_package(tmp_path)
    path = tmp_path / "config" / "system_config.yaml"
    path.write_text(
        "config_version: 1\nscreen_awareness:\n  check_interval_minutes: 20\nproactive:\n  timer_seconds: 40\n",
        encoding="utf-8",
    )
    boundary = ScreenAwarenessSettingsBoundary(GENERATION_ID, GENERATION_CREDENTIAL, tmp_path)
    settings = boundary.handle(_request("screen_awareness.settings.get", {}))["payload"]["settings"]
    assert settings["timerSeconds"] == 40
    assert "checkIntervalMinutes" not in settings


def test_nonpersonal_identity_keeps_minutes_and_rejects_a_personal_save(tmp_path: Path) -> None:
    path = tmp_path / "config" / "system_config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "config_version: 1\n"
        "screen_awareness:\n"
        "  check_interval_minutes: 20\n"
        "proactive:\n"
        "  timer_seconds: 40\n",
        encoding="utf-8",
    )
    clock = Clock()
    clock.now = 100
    runtime = _runtime(clock, personal=False)
    boundary = _boundary(tmp_path, runtime)
    shown = boundary.handle(_request("screen_awareness.settings.get", {}))["payload"]["settings"]
    assert shown["checkIntervalMinutes"] == 20
    assert "timerSeconds" not in shown
    before = path.read_bytes()
    rejected = boundary.handle(_request(
        "screen_awareness.settings.save",
        {"settings": {
            "enabled": True,
            "timerSeconds": 40,
            "cooldownSeconds": 600,
            "focusSettleDelay": 15,
            "windowSwitchCooldown": 60,
            "pollIntervalSeconds": 5,
        }},
    ))
    assert rejected["error"]["code"] == "INVALID_REQUEST"
    assert path.read_bytes() == before

    def advance() -> dict[str, object]:
        return _advance(boundary, SNAPSHOT)

    assert advance()["reason"] == "silence"
    clock.now = 130
    assert advance()["trigger"] == ""
    clock.now = 180
    assert advance()["trigger"] == ""


def test_personal_focus_settle_silence_privacy_and_away_follow_config(tmp_path: Path) -> None:
    path = tmp_path / "config" / "system_config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "config_version": 1,
                "screen_awareness": {"check_interval_minutes": 20, "cooldown_minutes": 10},
                "proactive": {
                    "enabled": True,
                    "timer_seconds": 8000,
                    "cooldown_seconds": 30,
                    "min_silence_after_user": 10,
                    "focus_settle_delay": 4,
                    "window_switch_cooldown": 5,
                    "idle_threshold_seconds": 40,
                    "away_max_seconds": 20,
                    "privacy": {"blocked_processes": [], "blocked_title_keywords": []},
                },
            }
        ),
        encoding="utf-8",
    )
    clock = Clock()
    idle_seconds = {"value": 0.0}
    runtime = _runtime(clock, personal=True, idle_seconds=lambda: idle_seconds["value"])
    boundary = _boundary(tmp_path, runtime)
    clock.now = 12
    assert _advance(boundary, SNAPSHOT)["reason"] != "silence"
    clock.now = 13
    switched = {
        "hwnd": 8,
        "pid": 8,
        "process": "1password.exe",
        "title": "vault",
        "ownProcess": False,
    }
    assert _advance(boundary, switched)["trigger"] == ""
    clock.now = 18
    ready = _advance(boundary, switched)
    assert ready["trigger"] == "window"
    assert ready["reason"] == "ready"
    clock.now = 19
    mail = {"hwnd": 9, "pid": 9, "process": "mail.exe", "title": "inbox", "ownProcess": False}
    assert _advance(boundary, mail)["trigger"] == ""
    clock.now = 24
    assert _advance(boundary, mail)["trigger"] == "window"
    settled = boundary.handle(_request(
        "screen_awareness.focus.advance",
        {"busy": False, "scope": GENERATION_ID, "outcome": "submitted"},
    ))
    assert settled["payload"]["reason"] == "submitted"

    clock.now = 25
    runtime._initiative.mark_screen_spoken(relationship_motive=False)
    _advance(boundary, mail)
    assert runtime.screen_gate_reason() == "cooldown"
    clock.now = 56
    _advance(boundary, mail)
    assert runtime.screen_gate_reason() == "eligible"

    runtime.set_screen_away(True)
    assert _advance(boundary, mail)["reason"] == "away"
    clock.now = 76
    assert _advance(boundary, mail)["reason"] != "away"
    idle_seconds["value"] = 50
    clock.now = 77
    assert _advance(boundary, mail)["trigger"] == "idle"


@pytest.mark.parametrize("section_name", ["screen_awareness", "proactive"])
def test_personal_save_retains_effective_privacy_and_unknown_privacy_fields(tmp_path, section_name):
    path = tmp_path / "config" / "system_config.yaml"
    path.parent.mkdir()
    privacy = {
        "blocked_processes": [],
        "blocked_title_keywords": ["private-fixture"],
        "future_policy": "keep",
    }
    path.write_text(yaml.safe_dump({
        "config_version": 1,
        section_name: {"enabled": False, "privacy": privacy},
    }), encoding="utf-8")
    boundary = _boundary(tmp_path, _runtime(Clock(), personal=True))
    current = boundary.handle(_request("screen_awareness.settings.get", {}))["payload"]["settings"]
    saved = boundary.handle(_request("screen_awareness.settings.save", {"settings": current}))
    assert saved["ok"] is True
    assert saved["payload"]["settings"]["enabled"] is False
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert document["proactive"]["privacy"] == privacy
    shell_privacy = boundary.handle(_request("screen_awareness.privacy.get", {}))
    assert shell_privacy["payload"]["blockedProcesses"] == []
    assert shell_privacy["payload"]["blockedTitleKeywords"] == ["private-fixture"]


def test_personal_shell_privacy_uses_the_same_authority_as_focus(tmp_path):
    path = tmp_path / "config" / "system_config.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump({
        "config_version": 1,
        "screen_awareness": {"privacy": {"blocked_processes": ["old.exe"]}},
        "proactive": {"privacy": {"blocked_processes": [], "blocked_title_keywords": ["private-fixture"]}},
    }), encoding="utf-8")
    boundary = _boundary(tmp_path, _runtime(Clock(), personal=True))
    result = boundary.handle(_request("screen_awareness.privacy.get", {}))
    assert result["payload"]["blockedProcesses"] == []
    assert result["payload"]["blockedTitleKeywords"] == ["private-fixture"]


def test_existing_unreadable_canonical_proactive_path_does_not_fall_back(tmp_path):
    legacy = tmp_path / "data" / "config" / "system_config.yaml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("proactive:\n  timer_seconds: 40\n", encoding="utf-8")
    canonical = tmp_path / "config" / "system_config.yaml"
    canonical.mkdir(parents=True)
    boundary = _boundary(tmp_path, _runtime(Clock(), personal=True))
    result = boundary.handle(_request("screen_awareness.settings.get", {}))
    assert result["ok"] is False
    assert result["error"]["code"] == "CONFIG_READ_ONLY"
    assert legacy.read_text(encoding="utf-8") == "proactive:\n  timer_seconds: 40\n"


def test_disabled_window_switch_keeps_the_personal_timer(tmp_path):
    path = tmp_path / "config" / "system_config.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump({
        "config_version": 1,
        "proactive": {
            "window_switch_enabled": False,
            "timer_seconds": 40,
            "focus_settle_delay": 4,
            "window_switch_cooldown": 0,
        },
    }), encoding="utf-8")
    clock = Clock()
    runtime = _runtime(clock, personal=True)
    boundary = _boundary(tmp_path, runtime)
    clock.now = 100
    assert _advance(boundary, SNAPSHOT)["trigger"] == ""
    clock.now = 110
    switched = {**SNAPSHOT, "hwnd": 8, "pid": 8}
    assert _advance(boundary, switched)["trigger"] == ""
    clock.now = 120
    assert _advance(boundary, switched)["trigger"] == ""
    clock.now = 150
    assert _advance(boundary, switched)["trigger"] == "timer"


def test_published_personal_observer_does_not_rederive_identity_from_pending_selection(tmp_path):
    import json
    from threading import Event

    from app.agent.tools import ToolRegistry
    from app.core_host.assistant_adapter import AssistantAdapter
    from test_personal_model_consumers import _root

    root = _root(tmp_path)
    manifest = next((root / "characters").glob("*/character.json"))
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["system_guards"] = "system_guards.md"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    (manifest.parent / "system_guards.md").write_text("SYNTHETIC_GUARDS", encoding="utf-8")
    (root / "config" / "system_config.yaml").write_text(
        "config_version: 1\nscreen_awareness:\n  enabled: false\n"
        "proactive:\n  enabled: true\n  min_silence_after_user: 0\n",
        encoding="utf-8",
    )
    adapter = AssistantAdapter(root, tool_registry=ToolRegistry(), mcp_provider=None)
    try:
        readiness = adapter.initialize(Event())
        runtime = readiness.session.runtime
        (root / "config" / "characters.yaml").write_text("current_character_id: missing\n", encoding="utf-8")
        assert runtime.screen_gate_reason() == "eligible"
    finally:
        adapter.close()
