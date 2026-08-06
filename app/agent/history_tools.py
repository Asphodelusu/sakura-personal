"""聊天历史查询工具：history_search / history_read。

长期记忆（memory_*）记住的是提炼后的事实；本模块查询的是原始对话记录。
"""

from __future__ import annotations

from typing import Any

from app.agent.time_awareness import format_relative_age, parse_relative_time_window
from app.storage.chat_history import ChatHistoryEntry, ChatHistoryStore

DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 50
SEARCH_CONTENT_CHARS = 120
READ_CONTENT_CHARS = 200
DEFAULT_CONTEXT_BEFORE = 3
DEFAULT_CONTEXT_AFTER = 3


class HistoryStoreRef:
    """可变 holder：工具 handler 闭包本对象，换角色时只更新 .store。"""

    def __init__(self, store: ChatHistoryStore | None = None) -> None:
        self.store = store


def handle_history_search(
    ref: HistoryStoreRef | None,
    arguments: dict[str, Any] | None,
) -> dict[str, Any]:
    """按时间范围（可选）+ 关键词（可选）定位历史消息；支持 offset 分页。"""
    args = arguments if isinstance(arguments, dict) else {}
    store = ref.store if ref is not None else None
    if store is None:
        return {
            "error": "聊天历史存储不可用。",
            "entries": [],
            "count": 0,
            "total_count": 0,
            "offset": 0,
            "limit": DEFAULT_SEARCH_LIMIT,
            "has_more": False,
        }

    time_text = str(args.get("time") or args.get("start") or "").strip()
    end_text = str(args.get("end") or "").strip()
    keyword = str(args.get("keyword") or args.get("query") or "").strip()
    limit = _clamp_int(args.get("limit"), DEFAULT_SEARCH_LIMIT, 1, MAX_SEARCH_LIMIT)
    offset = _clamp_int(args.get("offset"), 0, 0, 1_000_000)

    start_iso: str | None = None
    end_iso: str | None = None

    if time_text:
        window = parse_relative_time_window(time_text)
        if window is None:
            return {
                "error": (
                    f"无法解析时间「{time_text}」。"
                    "可用：昨天/今天/上周三、昨天下午/昨天晚上一点到两点、"
                    "N分钟前/约N小时前、YYYY-MM-DD/ISO。"
                ),
                "entries": [],
                "count": 0,
                "total_count": 0,
                "offset": offset,
                "limit": limit,
                "has_more": False,
            }
        start_iso, end_iso = window

    if end_text:
        end_window = parse_relative_time_window(end_text)
        if end_window is None:
            return {
                "error": f"无法解析结束时间「{end_text}」。",
                "entries": [],
                "count": 0,
                "total_count": 0,
                "offset": offset,
                "limit": limit,
                "has_more": False,
            }
        # end 参数：取解析窗口的终点（整天则当天末，相对则窗口 end）
        _end_start, end_bound = end_window
        if end_bound is not None:
            end_iso = end_bound
        elif _end_start is not None:
            end_iso = _end_start

    entries, has_more, total_count = store.search_between(
        start=start_iso,
        end=end_iso,
        keyword=keyword or None,
        limit=limit,
        offset=offset,
    )
    payload_entries = [
        _entry_payload(entry, max_chars=SEARCH_CONTENT_CHARS) for entry in entries
    ]
    next_offset = offset + len(payload_entries)
    result: dict[str, Any] = {
        "entries": payload_entries,
        "count": len(payload_entries),
        "total_count": total_count,
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
    }
    if not payload_entries:
        result["agent_hint"] = (
            "没有匹配的对话记录。可放宽时间/关键词，或先用 history_search 不带筛选看最近几条。"
        )
    elif has_more:
        result["next_offset"] = next_offset
        result["agent_hint"] = (
            f"还有更多（已返回 offset={offset} 起 {len(payload_entries)} 条，"
            f"共 total_count={total_count}）。"
            f"请用相同 time/keyword 再调用 history_search(offset={next_offset}, limit={limit}) 翻页，"
            "不要改关键词重搜；读完整段对话对 entry id 用 history_read。"
        )
    else:
        result["agent_hint"] = (
            f"已返回全部匹配（total_count={total_count}）。"
            "若需要某条前后完整上下文，对 entry id 调用 history_read。"
        )
    return result


def handle_history_read(
    ref: HistoryStoreRef | None,
    arguments: dict[str, Any] | None,
) -> dict[str, Any]:
    """以某条消息为锚点，向前/向后取上下文。"""
    args = arguments if isinstance(arguments, dict) else {}
    store = ref.store if ref is not None else None
    if store is None:
        return {
            "error": "聊天历史存储不可用。",
            "before": [],
            "target": None,
            "after": [],
            "anchor_id": 0,
            "count": 0,
            "has_more": False,
        }

    entry_id = _clamp_int(args.get("entry_id") or args.get("id"), 0, 0, 10**12)
    before = _clamp_int(args.get("before"), DEFAULT_CONTEXT_BEFORE, 0, 10)
    after = _clamp_int(args.get("after"), DEFAULT_CONTEXT_AFTER, 0, 10)

    raw = store.context_around(entry_id, before=before, after=after)
    before_entries = [
        _entry_payload(e, max_chars=READ_CONTENT_CHARS)
        for e in (raw.get("before") or [])
        if isinstance(e, ChatHistoryEntry)
    ]
    after_entries = [
        _entry_payload(e, max_chars=READ_CONTENT_CHARS)
        for e in (raw.get("after") or [])
        if isinstance(e, ChatHistoryEntry)
    ]
    target = raw.get("target")
    target_payload = (
        _entry_payload(target, max_chars=READ_CONTENT_CHARS)
        if isinstance(target, ChatHistoryEntry)
        else None
    )
    result: dict[str, Any] = {
        "before": before_entries,
        "target": target_payload,
        "after": after_entries,
        "anchor_id": raw.get("anchor_id", entry_id),
        "count": len(before_entries) + (1 if target_payload else 0) + len(after_entries),
        "has_more": False,
    }
    if raw.get("error"):
        result["error"] = raw["error"]
    if raw.get("hint"):
        result["agent_hint"] = raw["hint"]
    elif target_payload is None:
        result["agent_hint"] = "未找到锚点消息。"
    return result


def _entry_payload(entry: ChatHistoryEntry, *, max_chars: int) -> dict[str, Any]:
    return {
        "id": int(entry.id),
        "role": entry.role,
        "created_at": entry.created_at,
        "age": format_relative_age(entry.created_at),
        "content": _clip(entry.content, max_chars),
        "translation": _clip(entry.translation, max_chars) if entry.translation else "",
        "channel": entry.channel or "",
    }


def _clip(text: str, max_chars: int) -> str:
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    if max_chars <= 1:
        return "…"
    return value[: max_chars - 1] + "…"


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    if value is None or value == "":
        return default
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default
