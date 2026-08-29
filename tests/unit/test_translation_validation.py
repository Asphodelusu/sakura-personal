"""P1 source classification and Simplified Chinese validator v2."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.llm.translation_validation import (
    SourceKind,
    classify_translation_source,
    normalize_non_lexical_japanese,
    validate_lexical_chinese,
)


def test_ellipsis_sokuon_is_non_lexical_and_normalizes() -> None:
    classified = classify_translation_source("……っ。")
    assert classified.kind == SourceKind.NON_LEXICAL
    assert classified.local_zh == "……。"
    assert normalize_non_lexical_japanese("……っ。") == "……。"


def test_interjections_remain_lexical() -> None:
    assert classify_translation_source("あっ").kind == SourceKind.LEXICAL
    assert classify_translation_source("ん…").kind == SourceKind.LEXICAL
    assert classify_translation_source("あっ").local_zh == ""


def test_v2_rejects_empty_kana_echo_overlong_and_meta() -> None:
    assert validate_lexical_chinese("ねえ。", "", mode="v2").ok is False
    assert validate_lexical_chinese("ねえ。", "ねえ。", mode="v2").ok is False
    assert validate_lexical_chinese("おはよう。", "おはよう。", mode="v2").ok is False
    assert validate_lexical_chinese("おはよう。", "おはようだよ", mode="v2").ok is False
    assert validate_lexical_chinese("あ", "这是一段远远超过原文三倍长度的解释性中文说明文字。", mode="v2").ok is False
    assert validate_lexical_chinese("おはよう。", "翻译：早上好", mode="v2").ok is False
    assert validate_lexical_chinese("おはよう。", "以下是译文：早上好", mode="v2").ok is False
    assert validate_lexical_chinese("おはよう。", "Translation: morning", mode="v2").ok is False


def test_chouon_and_nakaguro_do_not_trigger_kana_reject() -> None:
    assert validate_lexical_chinese("コーヒー", "咖啡", mode="v2").ok is True
    assert validate_lexical_chinese("コーヒー", "咖啡ー", mode="v2").ok is True
    assert validate_lexical_chinese("サクラ", "樱・花", mode="v2").ok is True


def test_mixed_han_and_kana_is_rejected() -> None:
    result = validate_lexical_chinese("おはよう。", "早上おはよう", mode="v2")
    assert result.ok is False


def test_legacy_mode_restores_old_kana_rule() -> None:
    assert validate_lexical_chinese("コーヒー", "咖啡ー", mode="legacy").ok is False
    assert validate_lexical_chinese("おはよう。", "早上好", mode="legacy").ok is True
    assert validate_lexical_chinese("ねえ。", "", mode="legacy").ok is False


def test_provider_resolves_non_lexical_with_zero_requests() -> None:
    from app.llm.openai_translation_provider import OpenAITranslationProvider

    client = MagicMock()
    client.settings = SimpleNamespace(model="chat-fast-demo")
    client.complete_raw.return_value = "早上好"
    provider = OpenAITranslationProvider(client, max_attempts=2)
    assert provider.translate(["……っ。", "おはよう。"]) == ["……。", "早上好"]
    assert client.complete_raw.call_count == 1
    assert provider.last_metrics.resolved_indexes == (0, 1)
    assert provider.last_metrics.failed_indexes == ()
    assert provider.last_metrics.request_count == 1


def test_legacy_provider_does_not_apply_v2_non_lexical_shortcut() -> None:
    from app.llm.openai_translation_provider import OpenAITranslationProvider

    client = MagicMock()
    client.settings = SimpleNamespace(model="chat-fast-demo")
    client.complete_raw.return_value = "嗯"
    provider = OpenAITranslationProvider(client, max_attempts=1, validator_mode="legacy")

    assert provider.translate(["……っ。"]) == ["嗯"]
    assert client.complete_raw.call_count == 1


def test_new_translation_settings_default_to_v2(tmp_path) -> None:
    from app.config.settings_service import AppSettingsService
    from app.config.translation_settings import TranslationSettings

    service = AppSettingsService(tmp_path)
    loaded = service.load_translation_settings()
    assert loaded.validator_mode == "v2"
    service.save_system_values("ui", {"subtitle_language": "ja"})
    service.save_translation_settings(
        TranslationSettings(enabled=True, gate_timeout_seconds=6, max_attempts=2, validator_mode="legacy")
    )
    again = service.load_translation_settings()
    assert again.validator_mode == "legacy"
    from app.config.yaml_config import load_yaml_mapping

    system = load_yaml_mapping(service.system_config_path)
    assert system["ui"]["subtitle_language"] == "ja"
    assert system["translation"]["validator_mode"] == "legacy"
