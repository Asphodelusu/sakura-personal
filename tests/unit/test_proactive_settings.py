"""主动感知设置规范化与隐私黑名单接线。"""

from __future__ import annotations

from app.config.settings_service import AppSettingsService, normalize_proactive_config_mapping
from app.perception.privacy import (
    DEFAULT_BLOCKED_PROCESSES,
    PrivacyGuard,
    privacy_guard_from_mapping,
)


def test_normalize_proactive_config_fills_default_privacy() -> None:
    cfg = normalize_proactive_config_mapping({"enabled": True, "cooldown_seconds": 120})
    assert cfg["enabled"] is True
    assert cfg["cooldown_seconds"] == 120.0
    assert cfg["privacy"]["blocked_processes"] == list(DEFAULT_BLOCKED_PROCESSES)
    assert any("bitwarden" in k for k in cfg["privacy"]["blocked_title_keywords"])


def test_normalize_proactive_config_keeps_empty_blacklist() -> None:
    cfg = normalize_proactive_config_mapping(
        {
            "enabled": False,
            "privacy": {"blocked_processes": [], "blocked_title_keywords": []},
        }
    )
    assert cfg["privacy"]["blocked_processes"] == []
    assert cfg["privacy"]["blocked_title_keywords"] == []


def test_privacy_guard_empty_list_does_not_fallback_to_defaults() -> None:
    guard = PrivacyGuard(blocked_processes=[], blocked_title_keywords=[])
    assert guard.blocked_processes == []
    assert guard.blocked_title_keywords == []


def test_privacy_guard_from_mapping_uses_yaml_list() -> None:
    guard = privacy_guard_from_mapping(
        {"blocked_processes": ["Vault.exe"], "blocked_title_keywords": ["网银"]}
    )
    assert guard.blocked_processes == ["vault.exe"]
    assert guard.blocked_title_keywords == ["网银"]


def test_save_and_load_proactive_config_roundtrip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service = AppSettingsService(tmp_path)
    service.save_proactive_config(
        {
            "enabled": True,
            "cooldown_seconds": 333,
            "window_switch_enabled": False,
            "max_edge": 1280,
            "privacy": {
                "blocked_processes": ["keepassxc.exe"],
                "blocked_title_keywords": ["网上银行"],
            },
        }
    )
    loaded = service.load_proactive_config()
    assert loaded["cooldown_seconds"] == 333.0
    assert loaded["window_switch_enabled"] is False
    assert loaded["max_edge"] == 1280
    assert loaded["privacy"]["blocked_processes"] == ["keepassxc.exe"]
    assert loaded["privacy"]["blocked_title_keywords"] == ["网上银行"]
