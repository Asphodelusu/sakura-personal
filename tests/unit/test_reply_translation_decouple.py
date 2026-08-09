"""日语主回复与中文字幕解耦 — Phase 1。

红测先锁定：missing_translation 会触发第二次 Pro 结构化合成；
随后验证合格 segments 缺 zh 时可直接采用，并不再二次合成。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from app.agent.runtime import AgentRuntime
from app.llm.api_client import ChatMessage, OpenAICompatibleClient
from app.llm.chat_reply import ChatSegment, parse_chat_reply_result
from app.llm.translation_provider import FakeTranslationProvider, TranslationProvider
from app.storage.chat_history import ChatHistoryStore
from app.ui.subtitle_translation import (
    apply_translations_to_segments,
    patch_segment_list_by_text,
    segments_needing_translation,
    with_segment_translation,
)


def _dummy_system_prompt() -> str:
    return "你是 Sakura，一个桌宠助手。"


def _dummy_api_client() -> MagicMock:
    client = MagicMock(spec=OpenAICompatibleClient)
    client.resolve_dialogue_params.return_value = (0.8, {})
    return client


def test_missing_translation_currently_triggers_second_pro_compose() -> None:
    """现状：纯日语正文（无 zh）→ reason=missing_translation → 第二次 Pro 合成。"""
    client = _dummy_api_client()
    client.complete_with_tools.side_effect = [
        MagicMock(
            content="……開いたよ。北京の天気は曇りみたい。",
            tool_calls=[],
        ),
        MagicMock(
            content=json.dumps(
                {
                    "segments": [
                        {
                            "ja": "北京の天気を確認したよ。",
                            "zh": "我确认了北京天气。",
                            "tone": "中性",
                            "portrait": "站立待机",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            tool_calls=[],
        ),
    ]
    runtime = AgentRuntime(client, _dummy_system_prompt())

    reason = runtime._structured_compose_reason(
        "……開いたよ。北京の天気は曇りみたい。"
    )
    assert reason == "missing_translation"

    result = runtime.handle_user_message(
        [ChatMessage(role="user", content="北京天气")]
    )

    assert client.complete_with_tools.call_count == 2
    assert result.reply.segments[0].text == "北京の天気を確認したよ。"
    assert result.reply.segments[0].translation == "我确认了北京天气。"


def test_structured_segments_missing_zh_accepted_without_second_pro() -> None:
    """目标：首轮 JSON segments 仅有 ja/tone/portrait 时直接采用，不启动第二次 Pro。"""
    client = _dummy_api_client()
    first = json.dumps(
        {
            "segments": [
                {
                    "ja": "おはよう。",
                    "tone": "开心",
                    "portrait": "站立待机",
                }
            ]
        },
        ensure_ascii=False,
    )
    client.complete_with_tools.side_effect = [
        MagicMock(content=first, tool_calls=[]),
    ]
    runtime = AgentRuntime(client, _dummy_system_prompt())

    result = runtime.handle_user_message(
        [ChatMessage(role="user", content="早")]
    )

    assert client.complete_with_tools.call_count == 1
    segment = result.reply.segments[0]
    assert segment.text == "おはよう。"
    assert segment.tone == "开心"
    assert segment.portrait == "站立待机"
    assert segment.translation == ""


def test_parse_keeps_empty_translation_when_zh_missing() -> None:
    """解析层：缺 zh 时不得用日语原文填充 translation。"""
    raw = json.dumps(
        {
            "segments": [
                {"ja": "ねえ。", "tone": "温柔", "portrait": "站立待机"}
            ]
        },
        ensure_ascii=False,
    )
    parsed = parse_chat_reply_result(raw)
    assert parsed.ok is True
    assert parsed.needs_retry is False
    assert parsed.reply.segments[0].text == "ねえ。"
    assert parsed.reply.segments[0].translation == ""


def test_fake_translation_provider_satisfies_protocol() -> None:
    provider: TranslationProvider = FakeTranslationProvider(prefix="ZH:")
    assert provider.translate(["あ", "い"]) == ["ZH:あ", "ZH:い"]


def test_segments_needing_translation_and_apply() -> None:
    segments = [
        ChatSegment("あ", "中性", "", "站立待机"),
        ChatSegment("い", "中性", "已有", "站立待机"),
        ChatSegment("う", "开心", "", "伸手命令"),
    ]
    pending = segments_needing_translation(segments)
    assert [index for index, _ in pending] == [0, 2]
    updated = apply_translations_to_segments(
        segments,
        segment_indexes=[0, 2],
        translations=["啊", "呜"],
    )
    assert updated[0].translation == "啊"
    assert updated[1].translation == "已有"
    assert updated[2].translation == "呜"


def test_patch_segment_list_and_display_fallback() -> None:
    segments = [ChatSegment("おはよう", "开心", "", "站立待机")]
    assert segments[0].display_text("zh") == "おはよう"
    assert patch_segment_list_by_text(segments, "おはよう", "早安") is True
    assert segments[0].translation == "早安"
    assert segments[0].display_text("zh") == "早安"
    assert with_segment_translation(segments[0], "早上好").translation == "早上好"


def test_history_store_update_translation(tmp_path: Path) -> None:
    store = ChatHistoryStore(tmp_path / "chat.jsonl")
    entry_id = store.append("assistant", "おはよう", "", "开心", "站立待机")
    assert entry_id > 0
    assert store.update_translation(entry_id, "早安") is True
    entries = store.load()
    assert entries[-1].translation == "早安"
    assert store.update_translation(999999, "x") is False


def test_stale_interaction_translation_is_discarded() -> None:
    """过期 interaction 的翻译结果不得写回字幕/历史。"""
    window = MagicMock()
    window.active_interaction_id = "new-turn"
    window._pending_subtitle_translation_interaction_id = "new-turn"
    window._apply_subtitle_translations = MagicMock()

    from app.ui.pet_window import PetWindow

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
