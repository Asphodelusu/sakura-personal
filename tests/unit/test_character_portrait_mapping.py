from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from app.config.character_loader import (
    CharacterConfigError,
    CharacterProfile,
    _load_emotion_portrait_map,
    _load_tone_portrait_map,
    normalize_reply_portraits,
    resolve_reply_segment,
)
from app.llm.chat_reply import ChatReply, ChatSegment


@dataclass(frozen=True)
class _ThemeStub:
    primary_color: str = "#000000"


def _sakura_like_profile() -> CharacterProfile:
    package_dir = Path("characters/Sakura")
    expressions = {
        "站立待机": package_dir / "portraits/A020.png",
        "开心脸红": package_dir / "portraits/A061.png",
        "张嘴疑问": package_dir / "portraits/A070.png",
        "害羞脸红": package_dir / "portraits/A081.png",
        "不满无语": package_dir / "portraits/A140.png",
        "吃醋不满": package_dir / "portraits/A230.png",
        "伸手命令": package_dir / "portraits/I010.png",
    }
    return CharacterProfile(
        id="Sakura",
        display_name="夜乃桜",
        package_dir=package_dir,
        card_path=package_dir / "card.md",
        initial_message="……",
        default_portrait_path=expressions["站立待机"],
        expression_portraits=expressions,
        tone_portrait_map={
            "中性": "站立待机",
            "不满": "不满无语",
            "害羞": "害羞脸红",
            "请求": "伸手命令",
            "惊讶": "张嘴疑问",
        },
        emotion_portrait_map={
            "neutral": "站立待机",
            "angry": "吃醋不满",
            "happy": "开心脸红",
            "embarrassed": "害羞脸红",
        },
        reply_tones=["中性", "不满", "害羞", "请求", "惊讶"],
        theme_settings=_ThemeStub(),  # type: ignore[arg-type]
    )


def test_resolve_portrait_label_prefers_tone_over_default_portrait() -> None:
    profile = _sakura_like_profile()
    assert profile.resolve_portrait_label("站立待机", "害羞") == "害羞脸红"


def test_resolve_portrait_label_uses_alias_substring() -> None:
    profile = _sakura_like_profile()
    assert profile.resolve_portrait_label("害羞", "中性") == "害羞脸红"


def test_resolve_reply_segment_overrides_default_portrait_with_tone() -> None:
    profile = _sakura_like_profile()
    segment = ChatSegment(
        "また10分遅刻だ。",
        "不满",
        "你又迟到十分钟了。",
        "站立待机",
    )
    resolved = resolve_reply_segment(segment, profile)
    assert resolved.portrait == "不满无语"


def test_normalize_reply_portraits_fills_missing_portrait_from_tone() -> None:
    profile = _sakura_like_profile()
    reply = ChatReply(
        [
            ChatSegment("うん。", "不满", "嗯。", ""),
        ]
    )
    normalized = normalize_reply_portraits(reply, profile)
    assert normalized.segments[0].portrait == "不满无语"


def test_normalize_reply_portraits_uses_emotion_scorer_for_default_portrait() -> None:
    profile = _sakura_like_profile()
    reply = ChatReply(
        [
            ChatSegment(
                "また見てるの？",
                "中性",
                "又在看别人啦？气死我了。",
                "站立待机",
            )
        ]
    )
    normalized = normalize_reply_portraits(reply, profile)
    assert normalized.segments[0].portrait == "吃醋不满"


def test_portrait_selection_hints_lists_tone_and_emotion_groups() -> None:
    profile = _sakura_like_profile()
    hints = profile.portrait_selection_hints
    assert "tone「害羞」→ portrait「害羞脸红」" in hints
    assert "害羞" in hints


def test_load_tone_portrait_map_falls_back_to_global_table() -> None:
    labels = {"站立待机", "不满无语", "伸手命令"}
    result = _load_tone_portrait_map(
        {},
        reply_tones=["中性", "不满", "请求"],
        expression_labels=labels,
    )
    assert result == {
        "中性": "站立待机",
        "不满": "不满无语",
        "请求": "伸手命令",
    }


def test_load_emotion_portrait_map_rejects_unknown_emotion() -> None:
    with pytest.raises(CharacterConfigError, match="未知 emotion"):
        _load_emotion_portrait_map(
            {"emotion_map": {"love": "站立待机"}},
            expression_labels={"站立待机"},
        )
