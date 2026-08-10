"""中文字幕异步回填辅助 — 与 TTS/主回复解耦。"""

from __future__ import annotations

from dataclasses import dataclass

from app.llm.chat_reply import ChatSegment


@dataclass(frozen=True)
class PendingSubtitleTranslation:
    """一轮回复中待翻译的 segment 与历史 id 对应关系。"""

    interaction_id: str
    texts: tuple[str, ...]
    history_ids: tuple[int, ...]
    segment_indexes: tuple[int, ...]  # 相对本轮 clean_segments 的下标


def segments_needing_translation(segments: list[ChatSegment]) -> list[tuple[int, ChatSegment]]:
    """返回 (index, segment)，仅收录有日语正文且缺 zh 的项。"""
    pending: list[tuple[int, ChatSegment]] = []
    for index, segment in enumerate(segments):
        if not str(segment.text or "").strip():
            continue
        if str(segment.translation or "").strip():
            continue
        pending.append((index, segment))
    return pending


def with_segment_translation(segment: ChatSegment, translation: str) -> ChatSegment:
    return ChatSegment(
        segment.text,
        segment.tone,
        str(translation or "").strip(),
        segment.portrait,
        suppress_tts=segment.suppress_tts,
    )


def apply_translations_to_segments(
    segments: list[ChatSegment],
    *,
    segment_indexes: list[int] | tuple[int, ...],
    translations: list[str],
) -> list[ChatSegment]:
    """按本轮下标回填译文；长度不齐时忽略多余项。"""
    if not segments or not segment_indexes or not translations:
        return list(segments)
    updated = list(segments)
    for offset, index in enumerate(segment_indexes):
        if offset >= len(translations):
            break
        if index < 0 or index >= len(updated):
            continue
        text = str(translations[offset] or "").strip()
        if not text:
            continue
        updated[index] = with_segment_translation(updated[index], text)
    return updated


def patch_segment_list_by_text(
    segments: list[ChatSegment] | None,
    text: str,
    translation: str,
) -> bool:
    """就地替换列表中首个匹配日语正文的 segment 译文。返回是否改动。"""
    if not segments:
        return False
    target = str(text or "").strip()
    zh = str(translation or "").strip()
    if not target or not zh:
        return False
    for index, segment in enumerate(segments):
        if str(segment.text or "").strip() != target:
            continue
        if str(segment.translation or "").strip() == zh:
            return False
        segments[index] = with_segment_translation(segment, zh)
        return True
    return False
