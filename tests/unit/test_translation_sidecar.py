"""Translation sidecar P2 — settings, provider, schedule, bootstrap."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.agent.actions import AgentResult
from app.config.settings_service import AppSettingsService
from app.config.yaml_config import load_yaml_mapping
from app.llm.chat_reply import ChatReply, ChatSegment
from app.llm.translation_provider import FakeTranslationProvider
from app.ui.pet_window import PetWindow


def test_translation_settings_defaults_are_safe_when_section_missing(tmp_path: Path) -> None:
    service = AppSettingsService(tmp_path)
    settings = service.load_translation_settings()
    assert settings.enabled is False
    assert settings.gate_timeout_seconds == 6
    assert settings.max_attempts == 2


def test_translation_settings_round_trip(tmp_path: Path) -> None:
    from app.config.translation_settings import TranslationSettings

    service = AppSettingsService(tmp_path)
    service.save_system_values("ui", {"subtitle_language": "ja"})
    service.save_translation_settings(
        TranslationSettings(enabled=True, gate_timeout_seconds=8, max_attempts=2)
    )
    loaded = service.load_translation_settings()
    assert loaded.enabled is True
    assert loaded.gate_timeout_seconds == 8
    assert loaded.max_attempts == 2
    system = load_yaml_mapping(service.system_config_path)
    assert system["ui"]["subtitle_language"] == "ja"
    assert system["translation"]["enabled"] is True


def test_existing_zh_makes_zero_provider_calls() -> None:
    provider = FakeTranslationProvider()
    window = SimpleNamespace(
        translation_provider=provider,
        translation_settings=SimpleNamespace(enabled=True, gate_timeout_seconds=6),
        subtitle_language="zh",
        active_interaction_id="turn-1",
        subtitle_controller=MagicMock(),
        _start_subtitle_translation_worker=MagicMock(),
    )

    PetWindow._schedule_subtitle_translations(
        window,
        [ChatSegment("おはよう。", "开心", "早安。", "站立待机")],
        history_ids=[1],
    )

    window._start_subtitle_translation_worker.assert_not_called()
    window.subtitle_controller.begin_translation_gate.assert_not_called()
    assert provider.calls == []


def test_missing_zh_in_chinese_mode_starts_translation() -> None:
    provider = FakeTranslationProvider()
    controller = MagicMock()
    window = SimpleNamespace(
        translation_provider=provider,
        translation_settings=SimpleNamespace(enabled=True, gate_timeout_seconds=6),
        subtitle_language="zh",
        active_interaction_id="turn-2",
        subtitle_controller=controller,
        _start_subtitle_translation_worker=MagicMock(),
    )

    PetWindow._schedule_subtitle_translations(
        window,
        [ChatSegment("おはよう。", "开心", "", "站立待机")],
        history_ids=[9],
    )

    window._start_subtitle_translation_worker.assert_called_once()
    kwargs = window._start_subtitle_translation_worker.call_args.kwargs
    assert kwargs["texts"] == ["おはよう。"]
    assert kwargs["interaction_id"] == "turn-2"
    controller.begin_translation_gate.assert_called_once()
    assert controller.begin_translation_gate.call_args.kwargs["timeout_seconds"] == 6


def test_japanese_mode_makes_zero_provider_calls() -> None:
    provider = FakeTranslationProvider()
    window = SimpleNamespace(
        translation_provider=provider,
        translation_settings=SimpleNamespace(enabled=True, gate_timeout_seconds=6),
        subtitle_language="ja",
        active_interaction_id="turn-3",
        subtitle_controller=MagicMock(),
        _start_subtitle_translation_worker=MagicMock(),
    )

    PetWindow._schedule_subtitle_translations(
        window,
        [ChatSegment("おはよう。", "开心", "", "站立待机")],
        history_ids=[1],
    )

    window._start_subtitle_translation_worker.assert_not_called()
    window.subtitle_controller.begin_translation_gate.assert_not_called()
    assert provider.calls == []


def test_disabled_or_missing_provider_does_not_start_translation() -> None:
    controller = MagicMock()
    window = SimpleNamespace(
        translation_provider=None,
        translation_settings=SimpleNamespace(enabled=False, gate_timeout_seconds=6),
        subtitle_language="zh",
        active_interaction_id="turn-4",
        subtitle_controller=controller,
        _start_subtitle_translation_worker=MagicMock(),
    )

    PetWindow._schedule_subtitle_translations(
        window,
        [ChatSegment("おはよう。", "开心", "", "站立待机")],
        history_ids=[1],
    )

    window._start_subtitle_translation_worker.assert_not_called()
    controller.begin_translation_gate.assert_not_called()


def test_consume_agent_result_schedules_translation_before_show() -> None:
    order: list[str] = []

    class Window:
        messages: list[dict[str, str]] = []
        _consume_agent_result = PetWindow._consume_agent_result

        def _log_interaction_stage(self, *_args: object, **_kwargs: object) -> None:
            return None

        def _record_assistant_reply_history(self, *_args: object, **_kwargs: object) -> list[int]:
            return [3]

        def _show_reply_segments(self, _segments: object) -> None:
            order.append("show")

        def _schedule_subtitle_translations(self, _segments: object, **_kwargs: object) -> None:
            order.append("schedule")

        def _apply_pending_action_from_result(self, _result: object) -> None:
            return None

    result = AgentResult(
        reply=ChatReply(segments=[ChatSegment("あ", "中性", "", "站立待机")]),
        actions=[],
    )
    Window()._consume_agent_result(result)
    assert order == ["schedule", "show"]


def test_success_patches_current_pending_history_and_releases_gate() -> None:
    history = MagicMock()
    current = ChatSegment("あ", "中性", "", "站立待机")
    pending = [ChatSegment("い", "中性", "", "站立待机")]
    controller = SimpleNamespace(
        pending_reply_segments=pending,
        queued_reply_segment_batches=[],
        current_segment=current,
        speech_timer=SimpleNamespace(isActive=lambda: False),
        speech_index=0,
        set_speech=MagicMock(),
        release_translation_gate=MagicMock(),
    )
    window = SimpleNamespace(
        history_store=history,
        reply_history_segments=[current, pending[0]],
        subtitle_controller=controller,
        subtitle_language="zh",
    )

    PetWindow._apply_subtitle_translations(
        window,
        texts=["あ", "い"],
        translations=["啊", "嗯"],
        history_ids=[1, 2],
    )

    history.update_translation.assert_any_call(1, "啊")
    history.update_translation.assert_any_call(2, "嗯")
    assert window.reply_history_segments[0].translation == "啊"
    assert controller.pending_reply_segments[0].translation == "嗯"
    assert controller.current_segment.translation == "啊"
    controller.release_translation_gate.assert_called()
    controller.set_speech.assert_called()
    shown = controller.set_speech.call_args.args[0]
    assert shown == "啊"
    assert "あ" not in shown


def test_current_translation_failure_releases_gate() -> None:
    controller = MagicMock()
    window = SimpleNamespace(
        subtitle_controller=controller,
        active_interaction_id="turn-now",
        _pending_subtitle_translation_interaction_id="turn-now",
    )
    PetWindow._on_subtitle_translation_failed(
        window,
        {"error": "provider_failed", "interaction_id": "turn-now"},
    )
    controller.release_translation_gate.assert_called_once_with(fallback=True)


def test_stale_translation_failure_does_not_release_newer_gate() -> None:
    controller = MagicMock()
    window = SimpleNamespace(
        subtitle_controller=controller,
        active_interaction_id="turn-new",
        _pending_subtitle_translation_interaction_id="turn-new",
    )
    PetWindow._on_subtitle_translation_failed(
        window,
        {"error": "provider_failed", "interaction_id": "turn-old"},
    )
    controller.release_translation_gate.assert_not_called()


def test_refresh_llm_clients_rebuilds_translation_provider_from_new_chat_fast(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    from app.config.translation_settings import TranslationSettings
    from app.llm.openai_translation_provider import OpenAITranslationProvider
    from app.llm.slot_clients import AppLlmClients

    old_client = MagicMock(name="old_chat_fast")
    new_client = MagicMock(name="new_chat_fast")
    stale_provider = OpenAITranslationProvider(old_client, max_attempts=2)
    settings = TranslationSettings(enabled=True, gate_timeout_seconds=6, max_attempts=2)
    settings_service = MagicMock()
    settings_service.load_translation_settings.return_value = settings
    settings_service.load_inner_thought_settings.return_value = object()
    api_client = MagicMock()
    window = SimpleNamespace(
        settings_service=settings_service,
        api_client=api_client,
        agent_runtime=MagicMock(),
        memory_curator=MagicMock(),
        memory_reflector=None,
        memory_store=MagicMock(),
        translation_settings=settings,
        translation_provider=stale_provider,
        _restart_proactive_observer=lambda: None,
        start_llm_connection_warmup=lambda: None,
    )
    clients = AppLlmClients(
        chat=MagicMock(),
        chat_fast=new_client,
        vision=MagicMock(),
        memory_curation=MagicMock(),
        inner_thought=MagicMock(),
    )
    monkeypatch.setattr("app.llm.slot_clients.build_app_llm_clients", lambda *_a, **_k: clients)
    monkeypatch.setattr("app.llm.slot_clients.resolve_chat_api_settings", lambda *_a, **_k: MagicMock())

    PetWindow._refresh_llm_clients_after_settings(window)

    assert window.translation_settings.enabled is True
    assert isinstance(window.translation_provider, OpenAITranslationProvider)
    assert window.translation_provider is not stale_provider
    assert window.translation_provider.client is new_client


def test_late_patch_grace_defaults_clamps_and_zero_is_history_only(tmp_path: Path) -> None:
    from app.config.translation_settings import TranslationSettings

    service = AppSettingsService(tmp_path)
    loaded = service.load_translation_settings()
    assert loaded.late_patch_grace_ms == 1200

    service.save_system_values("translation", {"custom_sibling": "keep-me"})
    service.save_translation_settings(
        TranslationSettings(
            enabled=True,
            gate_timeout_seconds=6,
            max_attempts=2,
            late_patch_grace_ms=99999,
        )
    )
    clamped = service.load_translation_settings()
    assert clamped.late_patch_grace_ms == 10000
    system = load_yaml_mapping(service.system_config_path)
    assert system["translation"]["custom_sibling"] == "keep-me"
    assert system["translation"]["late_patch_grace_ms"] == 10000

    service.save_translation_settings(
        TranslationSettings(enabled=True, late_patch_grace_ms=0)
    )
    zero = service.load_translation_settings()
    assert zero.late_patch_grace_ms == 0


def test_worker_emits_first_serial_index_before_provider_finishes_remaining() -> None:
    import threading

    import pytest
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    qtwidgets = pytest.importorskip("PySide6.QtWidgets")
    if not hasattr(qtwidgets, "QApplication"):
        pytest.skip("当前测试环境只提供了 PySide6 stub。")
    app = QApplication.instance() or QApplication([])
    assert app is not None

    from app.llm.openai_translation_provider import TranslationBatchResult, TranslationIndexResult
    from app.ui.pet_window import SubtitleTranslationWorker

    first_seen = threading.Event()
    release_rest = threading.Event()
    completed = threading.Event()
    received: list[dict] = []

    class _BlockingIndexedProvider:
        def translate_indexed(self, texts, *, on_item=None, on_failed=None, **_kwargs):
            _ = texts, on_failed
            if on_item is not None:
                on_item(TranslationIndexResult(index=0, translation="早安。"))
            first_seen.set()
            assert release_rest.wait(timeout=2.0)
            if on_item is not None:
                on_item(TranslationIndexResult(index=1, translation="喂。"))
            completed.set()
            return TranslationBatchResult(
                items=(
                    TranslationIndexResult(index=0, translation="早安。"),
                    TranslationIndexResult(index=1, translation="喂。"),
                ),
                failed_indexes=(),
                request_count=2,
            )

        def translate(self, texts, **_kwargs):
            raise AssertionError("worker must call translate_indexed(), not translate()")

    worker = SubtitleTranslationWorker(
        _BlockingIndexedProvider(),
        interaction_id="turn-stream",
        texts=["おはよう。", "ねえ。"],
        history_ids=[11, 12],
        segment_indexes=[0, 1],
    )
    worker.index_resolved.connect(received.append, Qt.ConnectionType.DirectConnection)
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    assert first_seen.wait(timeout=1.0)
    assert len(received) == 1
    assert received[0]["interaction_id"] == "turn-stream"
    assert received[0]["segment_index"] == 0
    assert received[0]["history_id"] == 11
    assert received[0]["translation"] == "早安。"
    assert not completed.is_set()
    release_rest.set()
    thread.join(timeout=2.0)
    assert completed.is_set()
    assert [item["segment_index"] for item in received] == [0, 1]
    assert [item["translation"] for item in received] == ["早安。", "喂。"]


def test_identical_japanese_segments_receive_distinct_indexed_translations() -> None:
    history = MagicMock()
    first = ChatSegment("ねえ。", "温柔", "", "站立待机")
    second = ChatSegment("ねえ。", "温柔", "", "站立待机")
    controller = SimpleNamespace(
        pending_reply_segments=[second],
        queued_reply_segment_batches=[],
        current_segment=first,
        current_segment_index=0,
        speech_timer=SimpleNamespace(isActive=lambda: False),
        speech_index=0,
        set_speech=MagicMock(),
        release_translation_gate=MagicMock(),
        consume_index_success=None,
    )
    window = SimpleNamespace(
        history_store=history,
        reply_history_segments=[first, second],
        subtitle_controller=controller,
        subtitle_language="zh",
    )

    PetWindow._apply_subtitle_translations(
        window,
        texts=["ねえ。", "ねえ。"],
        translations=["喂。", "嘿。"],
        history_ids=[21, 22],
        segment_indexes=[0, 1],
    )

    history.update_translation.assert_any_call(21, "喂。")
    history.update_translation.assert_any_call(22, "嘿。")
    assert window.reply_history_segments[0].translation == "喂。"
    assert window.reply_history_segments[1].translation == "嘿。"
    assert controller.current_segment.translation == "喂。"
    assert controller.pending_reply_segments[0].translation == "嘿。"


def test_duplicate_index_callback_changes_ui_and_history_only_once() -> None:
    history = MagicMock()
    current = ChatSegment("あ", "中性", "", "站立待机")
    controller = SimpleNamespace(
        pending_reply_segments=[],
        queued_reply_segment_batches=[],
        current_segment=current,
        current_segment_index=0,
        speech_timer=SimpleNamespace(isActive=lambda: False),
        speech_index=0,
        set_speech=MagicMock(),
        release_translation_gate=MagicMock(),
    )
    window = SimpleNamespace(
        history_store=history,
        reply_history_segments=[current],
        subtitle_controller=controller,
        subtitle_language="zh",
        active_interaction_id="turn-dup",
        _pending_subtitle_translation_interaction_id="turn-dup",
    )
    payload = {
        "interaction_id": "turn-dup",
        "segment_index": 0,
        "history_id": 7,
        "text": "あ",
        "translation": "啊",
    }
    PetWindow._on_subtitle_translation_index_resolved(window, payload)
    PetWindow._on_subtitle_translation_index_resolved(window, payload)

    history.update_translation.assert_called_once_with(7, "啊")
    controller.set_speech.assert_called_once()


def test_stale_index_callback_does_not_change_bubble_or_history() -> None:
    history = MagicMock()
    current = ChatSegment("あ", "中性", "", "站立待机")
    set_speech = MagicMock()
    controller = SimpleNamespace(
        pending_reply_segments=[],
        queued_reply_segment_batches=[],
        current_segment=current,
        current_segment_index=0,
        set_speech=set_speech,
        release_translation_gate=MagicMock(),
    )
    window = SimpleNamespace(
        history_store=history,
        reply_history_segments=[current],
        subtitle_controller=controller,
        subtitle_language="zh",
        active_interaction_id="turn-new",
        _pending_subtitle_translation_interaction_id="turn-new",
    )
    PetWindow._on_subtitle_translation_index_resolved(
        window,
        {
            "interaction_id": "turn-old",
            "segment_index": 0,
            "history_id": 9,
            "text": "あ",
            "translation": "啊",
        },
    )
    history.update_translation.assert_not_called()
    set_speech.assert_not_called()
    assert controller.current_segment.translation == ""


def test_translation_target_tracks_portrait_resolved_display_segment() -> None:
    class _ResolvingProfile:
        id = "sakura"

        @staticmethod
        def default_portrait_label() -> str:
            return "站立待机"

        @staticmethod
        def resolve_portrait_label(
            portrait: str,
            tone: str,
            *,
            emotion: str | None = None,
        ) -> str:
            _ = portrait, tone, emotion
            return "平静认真脸"

    class _Controller:
        def __init__(self) -> None:
            self.current_segment = None
            self.current_segment_index = None
            self.pending_reply_segments = []
            self.queued_reply_segment_batches = []

        def show_segments(self, segments: list[ChatSegment]) -> None:
            self.current_segment = segments[0]
            self.current_segment_index = 0

        def consume_index_success(
            self,
            segment_index: int,
            updated: ChatSegment,
            *,
            is_current: bool | None = None,
        ) -> str:
            assert segment_index == 0
            assert is_current is True
            self.current_segment = updated
            return "revealed"

    original = ChatSegment("おはよう。", "认真", "", "未知立绘")
    older = ChatSegment("前の会話。", "中性", "之前的对话。", "站立待机")
    controller = _Controller()
    history = MagicMock()
    window = SimpleNamespace(
        translation_provider=FakeTranslationProvider(),
        translation_settings=SimpleNamespace(enabled=True, gate_timeout_seconds=6),
        subtitle_language="zh",
        active_interaction_id="turn-resolved-portrait",
        character_profile=_ResolvingProfile(),
        subtitle_controller=controller,
        history_store=history,
        reply_history_segments=[older],
        reply_history_index=0,
        _start_subtitle_translation_worker=MagicMock(),
        _cancel_backchannel=lambda: None,
        _exit_reply_history_review=lambda **_kwargs: None,
        _update_reply_history_buttons=lambda: None,
    )
    window._remember_reply_history_segments = (
        lambda segments: PetWindow._remember_reply_history_segments(window, segments)
    )

    PetWindow._schedule_subtitle_translations(window, [original], history_ids=[41])
    PetWindow._show_reply_segments(window, [original])
    displayed = controller.current_segment
    assert displayed is not original
    assert displayed.portrait == "平静认真脸"

    PetWindow._on_subtitle_translation_index_resolved(
        window,
        {
            "interaction_id": "turn-resolved-portrait",
            "segment_index": 0,
            "history_id": 41,
            "text": "おはよう。",
            "translation": "早安。",
        },
    )

    assert controller.current_segment.portrait == "平静认真脸"
    assert controller.current_segment.translation == "早安。"
    assert window.reply_history_segments[-1] is controller.current_segment
    assert window.reply_history_segments[0] is older
    history.update_translation.assert_called_once_with(41, "早安。")


def test_stale_interaction_translation_is_discarded() -> None:
    window = MagicMock()
    window.active_interaction_id = "new-turn"
    window._pending_subtitle_translation_interaction_id = "new-turn"
    window._apply_subtitle_translations = MagicMock()

    PetWindow._on_subtitle_translation_finished(
        window,
        {
            "interaction_id": "old-turn",
            "texts": ["あ"],
            "translations": ["啊"],
            "history_ids": [1],
        },
    )
    window._apply_subtitle_translations.assert_not_called()


def test_provider_uses_complete_raw_and_retries_invalid_output_once() -> None:
    from app.llm.openai_translation_provider import OpenAITranslationProvider

    client = MagicMock()
    client.settings = SimpleNamespace(model="chat-fast-demo", api_key="secret-key")
    client.complete_raw.side_effect = ["", "早上好"]
    provider = OpenAITranslationProvider(client, max_attempts=2)

    assert provider.translate(["おはよう。"]) == ["早上好"]
    assert client.complete_raw.call_count == 2
    for call in client.complete_raw.call_args_list:
        assert call.kwargs.get("max_attempts") == 1
        system_prompt = call.args[0]
        assert "简体中文" in system_prompt or "简体" in system_prompt
        assert "JSON" not in system_prompt


def test_provider_rejects_empty_or_japanese_after_retry() -> None:
    from app.llm.openai_translation_provider import OpenAITranslationProvider

    client = MagicMock()
    client.settings = SimpleNamespace(model="chat-fast-demo", api_key="secret-key")
    client.complete_raw.side_effect = ["", "おはよう。"]
    provider = OpenAITranslationProvider(client, max_attempts=2)

    try:
        provider.translate(["ねえ。"])
    except Exception:
        pass
    else:
        raise AssertionError("invalid translation must raise after retry")
    assert client.complete_raw.call_count == 2


def test_provider_logs_metadata_not_bodies_or_credentials(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.llm import openai_translation_provider as provider_module
    from app.llm.openai_translation_provider import OpenAITranslationProvider

    captured: list[tuple[str, str, object]] = []

    def fake_log(category: str, message: str, data: object = None) -> None:
        captured.append((category, message, data))

    monkeypatch.setattr(provider_module, "debug_log", fake_log)
    client = MagicMock()
    client.settings = SimpleNamespace(model="chat-fast-demo", api_key="secret-key")
    client.complete_raw.return_value = "早安。"
    provider = OpenAITranslationProvider(client, max_attempts=2)
    provider.translate(["おはよう。"])

    dumped = json.dumps(captured, ensure_ascii=False)
    assert "おはよう" not in dumped
    assert "早安" not in dumped
    assert "secret-key" not in dumped
    assert any(
        isinstance(item[2], dict)
        and item[2].get("model") == "chat-fast-demo"
        and item[2].get("outcome") == "success"
        and int(item[2].get("attempts") or 0) >= 1
        and "elapsed_ms" in item[2]
        for item in captured
    )


def test_bootstrap_defaults_to_disabled_unavailable_provider() -> None:
    import app.core.bootstrap as bootstrap
    from tests.unit.test_bootstrap import _build_startup_root

    root = _build_startup_root()
    context = bootstrap.build_initial_app_context(root)
    assert context.translation_settings.enabled is False
    assert context.translation_settings.gate_timeout_seconds == 6
    assert context.translation_settings.max_attempts == 2
    assert context.translation_provider is None


def test_bootstrap_injects_chat_fast_provider_when_enabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import app.core.bootstrap as bootstrap
    from app.config.translation_settings import TranslationSettings
    from app.llm.openai_translation_provider import OpenAITranslationProvider
    from app.llm.slot_clients import AppLlmClients
    from tests.unit.test_bootstrap import _build_startup_root

    root = _build_startup_root()
    fast_client = MagicMock()
    fast_client.settings = SimpleNamespace(model="fast-model")

    original_load = AppSettingsService.load_translation_settings

    def load_enabled(self: AppSettingsService) -> TranslationSettings:
        _ = original_load
        return TranslationSettings(enabled=True, gate_timeout_seconds=6, max_attempts=2)

    monkeypatch.setattr(AppSettingsService, "load_translation_settings", load_enabled)

    original_build = bootstrap.build_app_llm_clients

    def fake_clients(settings_service: object, *, base_settings: object = None) -> AppLlmClients:
        clients = original_build(settings_service, base_settings=base_settings)
        return AppLlmClients(
            chat=clients.chat,
            chat_fast=fast_client,
            vision=clients.vision,
            memory_curation=clients.memory_curation,
            inner_thought=clients.inner_thought,
        )

    monkeypatch.setattr(bootstrap, "build_app_llm_clients", fake_clients)
    context = bootstrap.build_initial_app_context(root)
    assert isinstance(context.translation_provider, OpenAITranslationProvider)
    assert context.translation_provider.client is fast_client


def test_bootstrap_enabled_without_chat_fast_stays_unavailable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import app.core.bootstrap as bootstrap
    from app.config.translation_settings import TranslationSettings
    from tests.unit.test_bootstrap import _build_startup_root

    root = _build_startup_root()
    monkeypatch.setattr(
        AppSettingsService,
        "load_translation_settings",
        lambda _self: TranslationSettings(enabled=True, gate_timeout_seconds=6, max_attempts=2),
    )
    context = bootstrap.build_initial_app_context(root)
    assert context.translation_provider is None
