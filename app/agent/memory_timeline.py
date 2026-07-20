"""memory_timeline — 记忆时间线工具。

按 memory_id 定位一条记忆，返回它在时间线上前后各 N 条上下文。
对应 claude-mem 的 timeline（Layer 2）。
"""

from __future__ import annotations

from typing import Any


DEFAULT_TIMELINE_BEFORE = 3
DEFAULT_TIMELINE_AFTER = 3


def build_timeline(
    memory_store: Any,
    memory_id: str,
    *,
    before: int = DEFAULT_TIMELINE_BEFORE,
    after: int = DEFAULT_TIMELINE_AFTER,
) -> dict[str, Any]:
    """以 memory_id 为锚点，返回前后时间线记忆。

    返回结构：
        {
            "before": [...],    # 时间上早于目标的记忆（最多 before 条）
            "target": {...},    # 目标记忆本身（包含完整 content）
            "after": [...],     # 时间上晚于目标的记忆（最多 after 条）
            "anchor_id": str,   # 回显请求的 id
        }

    不支持常驻档案（core_profile:...）作为锚点——若传入此类
    id，target 将为 None。
    若目标不存在，target 为 None，before/after 为空列表。
    """
    try:
        raw = memory_store.list_scope_memories(limit=500, wait=False)
    except Exception:
        return {
            "before": [],
            "target": None,
            "after": [],
            "anchor_id": memory_id,
            "error": "记忆列表加载失败",
        }

    if not raw:
        return {"before": [], "target": None, "after": [], "anchor_id": memory_id}

    # 按 created_at 排序；无时间戳的排最后
    def _sort_key(m: dict[str, Any]) -> str:
        return str(m.get("created_at") or m.get("updated_at") or "")

    sorted_memories = sorted(raw, key=_sort_key)

    # 定位目标
    idx = -1
    for i, m in enumerate(sorted_memories):
        mid = str(m.get("id") or m.get("memory_id") or "")
        if mid == memory_id:
            idx = i
            break

    if idx < 0:
        return {
            "before": [],
            "target": None,
            "after": [],
            "anchor_id": memory_id,
            "hint": "未找到该记忆，它可能已被删除、已放手、已过期，或是常驻档案（core_profile 不支持时间线）。",
        }

    target = sorted_memories[idx]
    before_slice = sorted_memories[max(0, idx - before):idx]
    after_slice = sorted_memories[idx + 1:idx + 1 + after]

    return {
        "before": [_compact(m) for m in before_slice],
        "target": _compact(target),
        "after": [_compact(m) for m in after_slice],
        "anchor_id": memory_id,
    }


def _compact(m: dict[str, Any]) -> dict[str, Any]:
    """从记忆记录提取时间线字段（id / title / content / created_at / layer）。

    title 与 memory._derive_title 一致，确保同一条记忆在
    index 和 timeline 里标题相同。
    """
    content = str(m.get("content") or m.get("memory") or "")
    metadata = m.get("metadata") if isinstance(m.get("metadata"), dict) else {}
    title = str(m.get("title") or metadata.get("title") or "").strip()
    if not title and content:
        max_chars = 44
        title = content[:max_chars].rstrip()
        if len(content) > max_chars:
            title = title[:max_chars - 3] + "…"
    return {
        "id": str(m.get("id") or ""),
        "title": title,
        "content": content,
        "created_at": str(m.get("created_at") or ""),
        "layer": str(m.get("layer") or ""),
    }
