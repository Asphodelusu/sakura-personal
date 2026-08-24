from pathlib import Path

from app.config.relationship_initiative import (
    EXPRESSION_BIASES,
    RelationshipInitiativeSettings,
    expression_bias_guidance,
)
from app.config.settings_service import AppSettingsService


def _service(tmp_path: Path) -> AppSettingsService:
    service = AppSettingsService(tmp_path)
    service.system_config_path.parent.mkdir(parents=True, exist_ok=True)
    return service


def test_defaults_enable_ab_and_natural() -> None:
    settings = RelationshipInitiativeSettings().normalized()
    assert settings.in_turn_enabled is True
    assert settings.proactive_enabled is True
    assert settings.expression_bias == "natural"
    assert settings.proactive_cooldown_seconds == 3600
    assert settings.proactive_min_silence_seconds == 300
    assert EXPRESSION_BIASES == ("restrained", "natural", "expressive")


def test_missing_yaml_section_uses_defaults(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.system_config_path.write_text("ui: {}\n", encoding="utf-8")
    loaded = service.load_relationship_initiative_settings()
    assert loaded == RelationshipInitiativeSettings().normalized()


def test_unknown_bias_and_invalid_times_normalize(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.system_config_path.write_text(
        """
relationship_initiative:
  in_turn_enabled: true
  proactive_enabled: false
  expression_bias: spicy
  proactive_cooldown_seconds: -9
  proactive_min_silence_seconds: "nope"
""".lstrip(),
        encoding="utf-8",
    )
    loaded = service.load_relationship_initiative_settings()
    assert loaded.proactive_enabled is False
    assert loaded.expression_bias == "natural"
    assert loaded.proactive_cooldown_seconds == 3600
    assert loaded.proactive_min_silence_seconds == 300


def test_times_clamp_to_safe_range() -> None:
    settings = RelationshipInitiativeSettings(
        proactive_cooldown_seconds=10,
        proactive_min_silence_seconds=10_000,
    ).normalized()
    assert settings.proactive_cooldown_seconds == 60
    assert settings.proactive_min_silence_seconds == 3600


def test_bias_guidance_has_no_content_blacklist() -> None:
    for bias in EXPRESSION_BIASES:
        text = expression_bias_guidance(bias)
        assert f"表达倾向：{bias}" in text
        assert "不得直接" not in text
        assert "最多只能" not in text
        assert "禁止" not in text
        assert "黑名单" not in text
    natural = expression_bias_guidance("natural")
    assert "平时克制" in natural
    assert "可以直接" in natural
    assert expression_bias_guidance("weird") == expression_bias_guidance("natural")


def test_save_roundtrip_preserves_unknown_sibling_keys(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.system_config_path.write_text("proactive:\n  enabled: true\n", encoding="utf-8")
    service.save_relationship_initiative_settings(
        RelationshipInitiativeSettings(
            in_turn_enabled=False,
            proactive_enabled=True,
            expression_bias="restrained",
            proactive_cooldown_seconds=1800,
            proactive_min_silence_seconds=120,
        )
    )
    text = service.system_config_path.read_text(encoding="utf-8")
    assert "proactive:" in text
    loaded = service.load_relationship_initiative_settings()
    assert loaded.in_turn_enabled is False
    assert loaded.expression_bias == "restrained"
    assert loaded.proactive_cooldown_seconds == 1800
    assert loaded.proactive_min_silence_seconds == 120
