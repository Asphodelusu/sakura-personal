"""P0 sidecar measurement: request purposes and bodyless index metrics."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.llm.payload_inspection import normalize_request_purpose


def test_subtitle_translation_purposes_are_known() -> None:
    assert normalize_request_purpose("subtitle_translation") == "subtitle_translation"
    assert normalize_request_purpose("subtitle_translation_retry") == "subtitle_translation_retry"
    assert normalize_request_purpose("compose") == "unknown"


def test_initial_and_retry_pass_correct_purpose() -> None:
    from app.llm.openai_translation_provider import OpenAITranslationProvider

    client = MagicMock()
    client.settings = SimpleNamespace(model="chat-fast-demo", api_key="secret-key")
    client.complete_raw.side_effect = ["", "早上好"]
    provider = OpenAITranslationProvider(client, max_attempts=2)

    assert provider.translate(["おはよう。"]) == ["早上好"]
    purposes = [
        str(call.kwargs.get("request_purpose") or "")
        for call in client.complete_raw.call_args_list
    ]
    assert purposes == ["subtitle_translation", "subtitle_translation_retry"]


def test_metrics_contain_indexes_counts_timing_and_no_text() -> None:
    from app.llm.openai_translation_provider import OpenAITranslationProvider

    client = MagicMock()
    client.settings = SimpleNamespace(model="chat-fast-demo", api_key="secret-key")
    client.complete_raw.return_value = "早上好"
    provider = OpenAITranslationProvider(client, max_attempts=2)
    provider.translate(["おはよう。", "ねえ。"])

    metrics = provider.last_metrics
    assert metrics is not None
    assert metrics.requested_indexes == (0, 1)
    assert metrics.resolved_indexes == (0, 1)
    assert metrics.failed_indexes == ()
    assert metrics.request_count == 2
    assert metrics.attempts == 2
    assert metrics.elapsed_ms >= 0
    dumped = json.dumps(metrics.__dict__, ensure_ascii=False)
    assert "おはよう" not in dumped
    assert "ねえ" not in dumped
    assert "早上好" not in dumped
    assert "secret-key" not in dumped
    assert "source" not in dumped
    assert "translation" not in dumped
    assert "zh" not in dumped
    assert "text" not in dumped


def test_failed_serial_batch_still_records_failed_indexes() -> None:
    from app.llm.openai_translation_provider import OpenAITranslationProvider, TranslationError

    client = MagicMock()
    client.settings = SimpleNamespace(model="chat-fast-demo")
    client.complete_raw.side_effect = ["早上好", "", ""]
    provider = OpenAITranslationProvider(client, max_attempts=2)
    try:
        provider.translate(["おはよう。", "ねえ。", "またね。"])
    except TranslationError:
        pass
    else:
        raise AssertionError("second invalid item must raise")
    metrics = provider.last_metrics
    assert metrics.requested_indexes == (0, 1, 2)
    assert metrics.resolved_indexes == (0,)
    assert metrics.failed_indexes == (1, 2)
    assert metrics.request_count >= 2
