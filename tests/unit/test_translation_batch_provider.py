"""P2 indexed head+tail batch: merge by index, isolate failures, bounded retry."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.config.settings_service import AppSettingsService
from app.llm.openai_translation_provider import (
    OpenAITranslationProvider,
    TranslationBatchResult,
    TranslationIndexResult,
)


def _client(side_effect) -> MagicMock:
    client = MagicMock()
    client.settings = SimpleNamespace(model="chat-fast-demo", api_key="secret-key")
    client.complete_raw.side_effect = side_effect
    return client


def _split_provider(client: MagicMock, **kwargs) -> OpenAITranslationProvider:
    return OpenAITranslationProvider(
        client,
        max_attempts=2,
        request_shape="split_batch",
        **kwargs,
    )


def test_one_lexical_segment_uses_one_request() -> None:
    client = _client(["早上好"])
    provider = _split_provider(client)
    result = provider.translate_indexed(["おはよう。"])
    assert client.complete_raw.call_count == 1
    assert result.request_count == 1
    assert result.failed_indexes == ()
    assert result.items == (
        TranslationIndexResult(index=0, translation="早上好", resolved_locally=False),
    )
    assert "JSON" not in client.complete_raw.call_args.args[0]
    assert client.complete_raw.call_args.kwargs.get("request_purpose") == "subtitle_translation"


def test_five_lexical_segments_use_two_initial_requests() -> None:
    seen: list[str] = []

    def complete_raw(system: str, messages: list[dict], **kwargs: object) -> str:
        user = str(messages[0]["content"])
        seen.append(user)
        if user.startswith("{"):
            payload = json.loads(user)
            items = [
                {"i": int(item["i"]), "zh": f"译{item['i']}"}
                for item in payload["items"]
            ]
            return json.dumps({"items": items}, ensure_ascii=False)
        return "译0"

    client = _client(complete_raw)
    provider = _split_provider(client)
    texts = ["あ。", "い。", "う。", "え。", "お。"]
    result = provider.translate_indexed(texts)
    assert client.complete_raw.call_count == 2
    assert result.request_count == 2
    assert [item.index for item in result.items] == [0, 1, 2, 3, 4]
    assert [item.translation for item in result.items] == ["译0", "译1", "译2", "译3", "译4"]
    assert result.failed_indexes == ()
    assert seen[0] == "あ。"
    tail = json.loads(seen[1])
    assert [item["i"] for item in tail["items"]] == [1, 2, 3, 4]


def test_head_on_item_fires_before_tail_completes() -> None:
    events: list[tuple[str, int]] = []

    def complete_raw(system: str, messages: list[dict], **kwargs: object) -> str:
        user = str(messages[0]["content"])
        if user.startswith("{"):
            events.append(("tail_request", client.complete_raw.call_count))
            return json.dumps({"items": [{"i": 1, "zh": "二"}]}, ensure_ascii=False)
        events.append(("head_request", 1))
        return "一"

    client = _client(complete_raw)
    provider = _split_provider(client)
    fired: list[int] = []

    def on_item(item: TranslationIndexResult) -> None:
        fired.append(item.index)
        events.append(("on_item", item.index))

    provider.translate_indexed(["あ。", "い。"], on_item=on_item)
    assert events[:3] == [("head_request", 1), ("on_item", 0), ("tail_request", 2)]
    assert fired == [0, 1]


def test_short_missing_extra_reordered_duplicate_merge_by_index() -> None:
    payload = {
        "items": [
            {"i": 2, "zh": "二"},
            {"i": 1, "zh": "一"},
            {"i": 1, "zh": "重复"},
            {"i": 9, "zh": "未知"},
            {"i": 3, "zh": "三"},
        ]
    }
    client = _client(["零", json.dumps(payload, ensure_ascii=False)])
    provider = _split_provider(client)
    result = provider.translate_indexed(["あ。", "い。", "う。", "え。"])
    by_index = {item.index: item.translation for item in result.items}
    assert by_index[0] == "零"
    assert by_index[1] == "一"
    assert by_index[2] == "二"
    assert by_index[3] == "三"
    assert 9 not in by_index
    assert result.failed_indexes == ()


def test_malformed_envelope_salvage_keeps_valid_indexes() -> None:
    raw = 'broken {"i":1,"zh":"嗯"} trailing'
    client = _client(["啊", raw])
    provider = _split_provider(client)
    result = provider.translate_indexed(["あ。", "い。"])
    assert [item.index for item in result.items] == [0, 1]
    assert result.items[1].translation == "嗯"
    assert result.failed_indexes == ()


def test_one_invalid_index_does_not_discard_four_valid() -> None:
    def complete_raw(system: str, messages: list[dict], **kwargs: object) -> str:
        user = str(messages[0]["content"])
        if kwargs.get("request_purpose") == "subtitle_translation_retry":
            return json.dumps({"items": [{"i": 2, "zh": "修好"}]}, ensure_ascii=False)
        if user.startswith("{"):
            return json.dumps(
                {
                    "items": [
                        {"i": 1, "zh": "一"},
                        {"i": 2, "zh": "おはよう。"},
                        {"i": 3, "zh": "三"},
                        {"i": 4, "zh": "四"},
                    ]
                },
                ensure_ascii=False,
            )
        return "零"

    client = _client(complete_raw)
    provider = _split_provider(client)
    result = provider.translate_indexed(["あ。", "い。", "う。", "え。", "お。"])
    assert result.request_count == 3
    by_index = {item.index: item.translation for item in result.items}
    assert by_index[0] == "零"
    assert by_index[1] == "一"
    assert by_index[2] == "修好"
    assert by_index[3] == "三"
    assert by_index[4] == "四"
    assert result.failed_indexes == ()


def test_retry_includes_failed_indexes_only_and_uses_retry_purpose() -> None:
    purposes: list[str] = []
    users: list[str] = []

    def complete_raw(system: str, messages: list[dict], **kwargs: object) -> str:
        purposes.append(str(kwargs.get("request_purpose") or ""))
        user = str(messages[0]["content"])
        users.append(user)
        if kwargs.get("request_purpose") == "subtitle_translation_retry":
            payload = json.loads(user)
            assert [item["i"] for item in payload["items"]] == [1]
            return json.dumps({"items": [{"i": 1, "zh": "重试成功"}]}, ensure_ascii=False)
        if user.startswith("{"):
            return json.dumps({"items": [{"i": 1, "zh": ""}]}, ensure_ascii=False)
        return "零"

    client = _client(complete_raw)
    provider = _split_provider(client)
    result = provider.translate_indexed(["あ。", "い。"])
    assert purposes == [
        "subtitle_translation",
        "subtitle_translation",
        "subtitle_translation_retry",
    ]
    assert result.request_count == 3
    assert result.items[1].translation == "重试成功"


def test_on_item_fires_at_most_once_per_index() -> None:
    client = _client(["一", json.dumps({"items": [{"i": 0, "zh": "重复头"}, {"i": 1, "zh": "二"}]})])
    provider = _split_provider(client)
    seen: list[int] = []
    provider.translate_indexed(["あ。", "い。"], on_item=lambda item: seen.append(item.index))
    assert seen == [0, 1]


def test_serial_adapter_stays_all_or_nothing() -> None:
    client = _client(["早上好", "", ""])
    provider = OpenAITranslationProvider(client, max_attempts=2, request_shape="serial")
    try:
        provider.translate(["おはよう。", "ねえ。"])
    except Exception:
        pass
    else:
        raise AssertionError("serial adapter must not return partial lists")
    assert client.complete_raw.call_count == 3


def test_serial_indexed_honors_request_shape_and_emits_each_plain_result() -> None:
    client = _client(["一", "二"])
    provider = OpenAITranslationProvider(client, max_attempts=2, request_shape="serial")
    seen: list[TranslationIndexResult] = []

    result = provider.translate_indexed(
        ["あ。", "い。"],
        on_item=seen.append,
    )

    assert result.items == (
        TranslationIndexResult(index=0, translation="一"),
        TranslationIndexResult(index=1, translation="二"),
    )
    assert seen == list(result.items)
    assert result.request_count == 2
    assert all(
        not str(call.args[1][0]["content"]).startswith("{")
        for call in client.complete_raw.call_args_list
    )
    assert all(
        call.kwargs.get("response_format") is None
        for call in client.complete_raw.call_args_list
    )


def test_request_shape_defaults_to_serial(tmp_path) -> None:
    service = AppSettingsService(tmp_path)
    settings = service.load_translation_settings()
    assert settings.request_shape == "serial"
    assert settings.validator_mode == "v2"


def test_concurrent_head_and_tail_use_barriers_when_enabled() -> None:
    start_head = threading.Event()
    start_tail = threading.Event()
    release = threading.Event()

    def complete_raw(system: str, messages: list[dict], **kwargs: object) -> str:
        user = str(messages[0]["content"])
        if user.startswith("{"):
            start_tail.set()
            assert start_head.wait(timeout=1)
            release.wait(timeout=1)
            return json.dumps({"items": [{"i": 1, "zh": "二"}]}, ensure_ascii=False)
        start_head.set()
        assert start_tail.wait(timeout=1)
        release.set()
        return "一"

    client = _client(complete_raw)
    provider = _split_provider(client, split_batch_concurrent=True)
    result = provider.translate_indexed(["あ。", "い。"])
    assert start_head.is_set() and start_tail.is_set()
    assert [item.translation for item in result.items] == ["一", "二"]
    assert isinstance(result, TranslationBatchResult)
    assert result.request_count == 2
