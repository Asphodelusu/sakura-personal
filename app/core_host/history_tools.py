"""history_search / history_read over the current character's Timeline.

Long-term memory keeps distilled facts; these tools read the raw dialogue.
Timeline ``seq`` plays the role of the Qt-era integer entry id.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from app.agent.time_awareness import format_relative_age, parse_iso_datetime, parse_relative_time_window
from app.agent.tools import Tool
from app.storage.paths import StoragePaths
from app.storage.timeline import TimelineDataError, TimelineEntry, TimelineKind, TimelineStore

DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 50
SEARCH_CONTENT_CHARS = 120
READ_CONTENT_CHARS = 200
DEFAULT_CONTEXT = 3
MAX_CONTEXT = 10
_ROLES = {TimelineKind.HUMAN: "user", TimelineKind.ASSISTANT: "assistant"}


def create_history_tools(user_root: Path, character_id: str) -> list[Tool]:
    history = _History(Path(user_root), character_id)
    return [
        Tool(
            name="history_search",
            description=(
                "查询原始对话记录（聊天流水，不是长期记忆）。"
                "默认不要用：问「认不认识 / 旧事 / 偏好 / 是谁」应用 memory_search。"
                "仅在需要逐字原话、按时间窗翻聊天记录，或对方明确要查「说过什么/聊天记录」时再用。"
                "可按相对时间（昨天/今天/约N小时前/YYYY-MM-DD 等）和/或关键词定位。"
                "有时间窗或关键词时按时间正序分页（从对话开头读）；"
                "返回 total_count/has_more，若 has_more 请用相同条件和 offset 翻页，不要改词重搜。"
                "无筛选时返回最近若干条。找到 id 后用 history_read 看前后上下文。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "time": {"type": "string", "description": (
                        "可选时间窗：昨天/今天/前天/上周三、昨天下午/昨天晚上、"
                        "昨天晚上一点到两点、N分钟前/约N小时前、YYYY-MM-DD/ISO。空=不限。")},
                    "end": {"type": "string", "description": "可选结束时间（同 time 格式）。通常只需 time。"},
                    "keyword": {"type": "string", "description": "可选关键词，匹配原文或译文。"},
                    "limit": {"type": "integer", "description": "每页条数，默认 20，上限 50。"},
                    "offset": {"type": "integer", "description": "分页偏移，默认 0。has_more 时用上次的 next_offset。"},
                },
            },
            handler=history.search,
            group="history",
        ),
        Tool(
            name="history_read",
            description=(
                "以某条对话消息为锚点，读取它前后的原始对话上下文。"
                "先用 history_search 找到 entry_id，再调用本工具展开。"
                "这是原始对话记录，不是长期记忆时间线（那是 memory_timeline）。"
                "仅在已判定需要原话/聊天上下文时使用；一般事实回忆用 memory_*。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "entry_id": {"type": "integer", "description": "锚点消息 id（来自 history_search）。"},
                    "before": {"type": "integer", "description": "锚点之前的条数，默认 3，上限 10。"},
                    "after": {"type": "integer", "description": "锚点之后的条数，默认 3，上限 10。"},
                },
                "required": ["entry_id"],
            },
            handler=history.read,
            group="history",
        ),
    ]


class _History:
    def __init__(self, user_root: Path, character_id: str) -> None:
        self._path = StoragePaths(user_root).timeline_database()
        self._character_id = character_id

    def _dialogue(self) -> list[TimelineEntry] | None:
        try:
            entries = TimelineStore(self._path).read_all(self._character_id)
        except TimelineDataError:
            return None
        return [entry for entry in entries if entry.kind in _ROLES]

    def search(self, arguments: dict[str, Any] | None) -> dict[str, Any]:
        args = arguments if isinstance(arguments, dict) else {}
        limit = _clamp_int(args.get("limit"), DEFAULT_SEARCH_LIMIT, 1, MAX_SEARCH_LIMIT)
        offset = _clamp_int(args.get("offset"), 0, 0, 1_000_000)
        empty = {"entries": [], "count": 0, "total_count": 0, "offset": offset, "limit": limit, "has_more": False}
        dialogue = self._dialogue()
        if dialogue is None:
            return {**empty, "error": "聊天历史存储不可用。"}

        time_text = str(args.get("time") or args.get("start") or "").strip()
        end_text = str(args.get("end") or "").strip()
        keyword = str(args.get("keyword") or args.get("query") or "").strip().casefold()
        start = end = None
        if time_text:
            window = parse_relative_time_window(time_text)
            if window is None:
                return {**empty, "error": (
                    f"无法解析时间「{time_text}」。"
                    "可用：昨天/今天/上周三、昨天下午/昨天晚上一点到两点、N分钟前/约N小时前、YYYY-MM-DD/ISO。")}
            start, end = _instant(window[0]), _instant(window[1])
        if end_text:
            end_window = parse_relative_time_window(end_text)
            if end_window is None:
                return {**empty, "error": f"无法解析结束时间「{end_text}」。"}
            end = _instant(end_window[1] if end_window[1] is not None else end_window[0])

        filtered = bool(start or end or keyword)
        matches = [
            entry for entry in dialogue
            if _within(entry, start, end)
            and (not keyword or keyword in _content(entry).casefold() or keyword in _translation(entry).casefold())
        ]
        total = len(matches)
        if filtered:
            page = matches[offset: offset + limit]
        else:
            newest_first = matches[::-1][offset: offset + limit]
            page = newest_first[::-1]
        entries = [_payload(entry, SEARCH_CONTENT_CHARS) for entry in page]
        has_more = offset + len(entries) < total
        result: dict[str, Any] = {
            "entries": entries, "count": len(entries), "total_count": total,
            "offset": offset, "limit": limit, "has_more": has_more,
        }
        if not entries:
            result["agent_hint"] = "没有匹配的对话记录。可放宽时间/关键词，或先用 history_search 不带筛选看最近几条。"
        elif has_more:
            result["next_offset"] = offset + len(entries)
            result["agent_hint"] = (
                f"还有更多（已返回 offset={offset} 起 {len(entries)} 条，共 total_count={total}）。"
                f"请用相同 time/keyword 再调用 history_search(offset={offset + len(entries)}, limit={limit}) 翻页，"
                "不要改关键词重搜；读完整段对话对 entry id 用 history_read。"
            )
        else:
            result["agent_hint"] = f"已返回全部匹配（total_count={total}）。若需要某条前后完整上下文，对 entry id 调用 history_read。"
        return result

    def read(self, arguments: dict[str, Any] | None) -> dict[str, Any]:
        args = arguments if isinstance(arguments, dict) else {}
        anchor = _clamp_int(args.get("entry_id") or args.get("id"), 0, 0, 10**12)
        before = _clamp_int(args.get("before"), DEFAULT_CONTEXT, 0, MAX_CONTEXT)
        after = _clamp_int(args.get("after"), DEFAULT_CONTEXT, 0, MAX_CONTEXT)
        result: dict[str, Any] = {"before": [], "target": None, "after": [], "anchor_id": anchor, "count": 0, "has_more": False}
        dialogue = self._dialogue()
        if dialogue is None:
            return {**result, "error": "聊天历史存储不可用。"}
        if anchor <= 0:
            return {**result, "error": "entry_id 无效，请先用 history_search 获取。"}
        position = next((index for index, entry in enumerate(dialogue) if entry.seq == anchor), None)
        if position is None:
            return {**result, "agent_hint": "未找到锚点消息。"}
        result["before"] = [_payload(entry, READ_CONTENT_CHARS) for entry in dialogue[max(0, position - before): position]]
        result["target"] = _payload(dialogue[position], READ_CONTENT_CHARS)
        result["after"] = [_payload(entry, READ_CONTENT_CHARS) for entry in dialogue[position + 1: position + 1 + after]]
        result["count"] = len(result["before"]) + 1 + len(result["after"])
        return result


def _instant(value: str | None) -> datetime | None:
    parsed = parse_iso_datetime(value) if value else None
    return parsed.astimezone() if parsed is not None else None


def _within(entry: TimelineEntry, start: datetime | None, end: datetime | None) -> bool:
    if start is None and end is None:
        return True
    created = _instant(entry.created_at)
    if created is None:
        return False
    return (start is None or created >= start) and (end is None or created <= end)


def _segments(entry: TimelineEntry) -> list[dict[str, Any]]:
    segments = entry.payload.get("segments")
    return [item for item in segments if isinstance(item, dict)] if isinstance(segments, list) else []


def _content(entry: TimelineEntry) -> str:
    if entry.kind is TimelineKind.ASSISTANT:
        return "".join(str(item.get("text") or "") for item in _segments(entry))
    return str(entry.payload.get("text") or "")


def _translation(entry: TimelineEntry) -> str:
    if entry.kind is TimelineKind.ASSISTANT:
        return "".join(str(item.get("translation") or "") for item in _segments(entry))
    return ""


def _payload(entry: TimelineEntry, max_chars: int) -> dict[str, Any]:
    translation = _translation(entry)
    return {
        "id": entry.seq,
        "role": _ROLES[entry.kind],
        "created_at": entry.created_at,
        "age": format_relative_age(entry.created_at),
        "content": _clip(_content(entry), max_chars),
        "translation": _clip(translation, max_chars) if translation else "",
        "channel": entry.origin or "",
    }


def _clip(text: str, max_chars: int) -> str:
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1] + "…"


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    if value is None or value == "":
        return default
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default
