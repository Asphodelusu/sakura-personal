from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.agent.memory import MemoryStore
from app.llm.prompts.types import ContextFragment, ContextRequest


DEFAULT_MEMORY_RECALL_LIMIT = 5
DEFAULT_MEMORY_RECALL_CANDIDATES = 10
# all-MiniLM 这类轻量嵌入模型的余弦相似度天然偏低（实测相关命中也常在 0.3~0.45），
# 0.5 会把所有候选都过滤掉、令自动召回形同虚设。用 0.3 作为去噪下限，配合 top-k=5
# 与按分排序，既挡住明显无关项，又能让最相关的少量记忆进入上下文。
DEFAULT_MEMORY_RELEVANCE_THRESHOLD = 0.3
MAX_MEMORY_QUERY_CHARS = 4000
# 时间衰减半衰期（天），≈ln(2)/λ。λ=0.1 时半衰期约 7 天。
DEFAULT_MEMORY_DECAY_LAMBDA = 0.1
# source=explicit（用户明确要求记住）的记忆自动提至此 importance
EXPLICIT_MEMORY_IMPORTANCE = 0.95
# 回忆强化：被 memory_search 命中后衰减计时重置
_ACCESS_TRACKER_LOCK = threading.Lock()
_ACCESS_TRACKER_PATH: Path | None = None
_ACCESS_TRACKER_CACHE: dict[str, str] = {}
_ACCESS_TRACKER_DIRTY: bool = False


def _set_access_tracker_path(path: Path) -> None:
    global _ACCESS_TRACKER_PATH
    _ACCESS_TRACKER_PATH = path


def _load_access_tracker() -> dict[str, str]:
    global _ACCESS_TRACKER_DIRTY
    if _ACCESS_TRACKER_PATH is None or not _ACCESS_TRACKER_PATH.exists():
        return {}
    try:
        with _ACCESS_TRACKER_LOCK:
            data = json.loads(_ACCESS_TRACKER_PATH.read_text(encoding="utf-8"))
            _ACCESS_TRACKER_CACHE = data if isinstance(data, dict) else {}
            _ACCESS_TRACKER_DIRTY = False
        return dict(_ACCESS_TRACKER_CACHE)
    except (OSError, json.JSONDecodeError):
        return {}


def _record_memory_accessed(memory_ids: list[str]) -> None:
    """批量记录记忆被访问时间，延迟落盘。"""
    global _ACCESS_TRACKER_DIRTY
    if _ACCESS_TRACKER_PATH is None or not memory_ids:
        return
    now_iso = datetime.now().astimezone().isoformat()
    with _ACCESS_TRACKER_LOCK:
        for mid in memory_ids:
            mid = str(mid).strip()
            if mid:
                _ACCESS_TRACKER_CACHE[mid] = now_iso
        _ACCESS_TRACKER_DIRTY = True


def _flush_access_tracker() -> None:
    """落盘访问记录。"""
    global _ACCESS_TRACKER_DIRTY
    if _ACCESS_TRACKER_PATH is None or not _ACCESS_TRACKER_DIRTY:
        return
    with _ACCESS_TRACKER_LOCK:
        if not _ACCESS_TRACKER_DIRTY:
            return
        try:
            _ACCESS_TRACKER_PATH.parent.mkdir(parents=True, exist_ok=True)
            _ACCESS_TRACKER_PATH.write_text(
                json.dumps(_ACCESS_TRACKER_CACHE, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _ACCESS_TRACKER_DIRTY = False
        except OSError:
            pass


def _get_last_accessed(memory_id: str) -> str:
    """返回记忆最后一次被访问的 ISO 时间，未追踪则返回空。"""
    if _ACCESS_TRACKER_PATH is None:
        return ""
    with _ACCESS_TRACKER_LOCK:
        return _ACCESS_TRACKER_CACHE.get(str(memory_id).strip(), "")


@dataclass(frozen=True)
class MemoryRecallResult:
    fragments: tuple[ContextFragment, ...] = ()
    status: str = "ready"
    query: str = ""


class MemoryRecallService:
    """基于本轮上下文选择少量相关长期记忆。"""

    def __init__(
        self,
        memory: MemoryStore,
        *,
        limit: int = DEFAULT_MEMORY_RECALL_LIMIT,
        threshold: float = DEFAULT_MEMORY_RELEVANCE_THRESHOLD,
    ) -> None:
        self.memory = memory
        self.limit = max(1, limit)
        self.threshold = threshold

    def recall(self, request: ContextRequest) -> MemoryRecallResult:
        query = _build_memory_query(request)
        if not query:
            return MemoryRecallResult(query="")

        # 初始化访问追踪文件路径
        if _ACCESS_TRACKER_PATH is None and self.memory.base_dir is not None:
            _set_access_tracker_path(
                self.memory.base_dir / "data" / "memory" / "access_tracker.json"
            )
        _load_access_tracker()

        try:
            response = self.memory.search_memory(
                {"query": query, "limit": DEFAULT_MEMORY_RECALL_CANDIDATES},
                wait=False,
            )
        except Exception:  # noqa: BLE001 - 记忆故障不得阻断普通聊天
            return MemoryRecallResult(status="failed", query=query)
        status = str(response.get("status", "ready"))
        memories = response.get("memories", [])
        if status != "ready" and not memories:
            return MemoryRecallResult(status=status, query=query)
        if not isinstance(memories, list):
            return MemoryRecallResult(status="failed", query=query)

        selected = _select_memories(memories, self.threshold, self.limit)

        # 回忆强化：记录被选中的记忆（被检索到即算访问）
        hit_ids = [m["id"] for m in selected if m.get("id")]
        _record_memory_accessed(hit_ids)
        _flush_access_tracker()

        fragments = tuple(
            ContextFragment(
                fragment_id=f"memory.{memory['id'] or index}",
                source="memory",
                # 不再逐条加「与本轮相关的长期记忆：」前缀——source=memory 属性已表明
                # 来源，让每条都带同样的开场白只会显得像检索结果堆砌，也白费 token。
                content=memory["content"],
                trust="trusted" if memory["source"] == "explicit" else "untrusted",
                priority=80 if memory["source"] == "explicit" else 70,
                freshness=memory["updated_at"],
                token_budget=512,
                sensitivity="private",
                cache_scope="turn",
            )
            for index, memory in enumerate(selected)
        )
        return MemoryRecallResult(fragments=fragments, status="ready", query=query)


def _build_memory_query(request: ContextRequest) -> str:
    parts: list[str] = []
    if request.current_input.strip():
        parts.append(request.current_input.strip())
    recent_user = [
        message.content.strip()
        for message in request.recent_messages
        if message.role == "user" and message.content.strip()
    ]
    parts.extend(recent_user[-2:])
    parts.extend(summary.strip() for summary in request.visual_summaries if summary.strip())
    unique = list(dict.fromkeys(parts))
    query = "\n".join(unique).strip()
    return query[:MAX_MEMORY_QUERY_CHARS].rstrip()


def _select_memories(
    memories: list[Any],
    threshold: float,
    limit: int,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    now = datetime.now().astimezone()
    for raw in memories:
        if not isinstance(raw, dict):
            continue
        content = str(raw.get("content") or raw.get("memory") or "").strip()
        if not content:
            continue
        dedupe_key = " ".join(content.lower().split())
        if dedupe_key in seen or _is_expired(raw.get("expires_at"), now):
            continue
        score = _optional_score(raw.get("score"))
        if score is not None and score < threshold:
            continue
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        source = str(raw.get("source") or metadata.get("source") or "inferred").strip().lower()
        updated_at = str(raw.get("updated_at") or metadata.get("updated_at") or "").strip()
        created_at = str(raw.get("created_at") or metadata.get("created_at") or "").strip()
        importance = _extract_importance(metadata, source)
        # 回忆强化：最近被 search 过的记忆，用访问时间代替更新时间算衰减
        last_accessed = _get_last_accessed(str(raw.get("id") or raw.get("memory_id") or ""))
        effective_ts = last_accessed or updated_at or created_at
        days = _days_since(effective_ts, now)
        decay_weight = _compute_decay_weight(importance, days)
        normalized.append(
            {
                "id": str(raw.get("id") or raw.get("memory_id") or "").strip(),
                "content": content,
                "score": score,
                "source": source,
                "updated_at": updated_at,
                "importance": importance,
                "decay_weight": decay_weight,
            }
        )
        seen.add(dedupe_key)
    normalized.sort(
        key=lambda item: (
            item["score"] is None,
            -(item["score"] if item["score"] is not None else -1.0) * item["decay_weight"],
            item["source"] != "explicit",
            item["updated_at"],
        )
    )
    return normalized[:limit]


def _optional_score(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _extract_importance(metadata: dict[str, Any], source: str) -> float:
    """从记忆元数据中提取重要性。explicit 来源的记忆自动提权。"""
    raw = metadata.get("importance")
    try:
        importance = float(raw) if raw is not None else 0.5
    except (TypeError, ValueError):
        importance = 0.5
    importance = max(0.0, min(1.0, importance))
    if source == "explicit":
        importance = max(importance, EXPLICIT_MEMORY_IMPORTANCE)
    return importance


def _days_since(iso_string: str, now: datetime) -> float:
    """从 ISO 时间字符串计算距今天数。解析失败返回 0。"""
    if not iso_string:
        return 0.0
    try:
        dt = datetime.fromisoformat(iso_string)
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=now.tzinfo)
    return max(0.0, (now - dt).total_seconds() / 86400.0)


def _compute_decay_weight(importance: float, days: float, lmbda: float = DEFAULT_MEMORY_DECAY_LAMBDA) -> float:
    """重要性加权时间衰减：importance=1.0 永不衰减，importance=0.2 快速衰减。"""
    decay = math.exp(-lmbda * days)
    return importance + (1.0 - importance) * decay


def _is_expired(value: Any, now: datetime) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        expires_at = datetime.fromisoformat(value.strip())
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=now.tzinfo)
    return expires_at <= now
