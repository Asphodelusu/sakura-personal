from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.agent.memory import MemoryStore, memory_record_is_reflection, _bounded_float
from app.agent.persona_state import (
    PersonaState,
    emotion_congruence_factor,
    resolve_persona_state,
)
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
# 约定/纪念日：event_time 落在今天或明天时主动浮现（不占满检索名额）
MAX_DUE_COMMITMENT_RECALLS = 2
DUE_COMMITMENT_DECAY_BOOST = 1.28
# 冷归档：久未激活且低重要性的琐碎记忆缓慢降权（豁免约定/情感转折）
COLD_ARCHIVE_IDLE_DAYS = 45
COLD_ARCHIVE_IMPORTANCE_MAX = 0.38
COLD_ARCHIVE_DECAY_FLOOR = 0.52
EXEMPT_COLD_ARCHIVE_KINDS = frozenset({"commitment", "emotional_turn", "core_profile"})
# 独处反思：自动召回降权弱注入（非硬过滤），每轮最多 1 条，避免抢事实位。
REFLECTION_AUTO_RECALL_SCORE_FACTOR = 0.3
MAX_REFLECTION_IN_AUTO_RECALL = 1


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

    def recall(self, request: ContextRequest, *, light_mode: bool = False) -> MemoryRecallResult:
        query = _build_memory_query(request)
        if not query:
            return MemoryRecallResult(query="")

        # 初始化访问追踪文件路径
        if _ACCESS_TRACKER_PATH is None and self.memory.base_dir is not None:
            _set_access_tracker_path(
                self.memory.base_dir / "data" / "memory" / "access_tracker.json"
            )
        _load_access_tracker()

        limit = DEFAULT_MEMORY_RECALL_CANDIDATES
        if light_mode:
            limit = 3  # 轻量模式只取少量候选

        try:
            response = self.memory.search_memory(
                {"query": query, "limit": limit},
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

        now = datetime.now().astimezone()
        persona = _resolve_recall_persona(self.memory, query)
        due_commitments = _select_due_commitment_memories(self.memory, now=now)
        # 实体扩展：从首轮结果中提取专有名词，追加相关记忆
        expanded = _expand_by_entities(memories, self.memory, self.threshold, self.limit)
        if expanded:
            memories = _deduplicate_memories(memories + expanded)

        select_limit = 2 if light_mode else self.limit
        selected = _select_memories(
            memories,
            self.threshold,
            select_limit,
            now=now,
            persona=persona,
        )
        selected = _merge_due_commitments(selected, due_commitments, self.limit)

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
                content=_annotate_recalled_memory_content(memory, now=now),
                trust="trusted" if memory["source"] == "explicit" else "untrusted",
                priority=(
                    45
                    if memory.get("is_reflection")
                    else (80 if memory["source"] == "explicit" else 70)
                ),
                freshness=memory["updated_at"],
                token_budget=280 if memory.get("is_reflection") else 512,
                sensitivity="private",
                cache_scope="turn",
            )
            for index, memory in enumerate(selected)
        )
        return MemoryRecallResult(fragments=fragments, status="ready", query=query)


def _annotate_recalled_memory_content(memory: dict[str, Any], *, now: datetime) -> str:
    from app.agent.memory import memory_record_is_expired
    from app.agent.time_awareness import annotate_with_relative_age, memory_event_timestamp

    content = str(memory.get("content") or "").strip()
    annotated = annotate_with_relative_age(
        content,
        memory_event_timestamp(memory),
        now=now,
        expired=memory_record_is_expired(memory, now=now),
    )
    if memory_record_is_reflection(memory):
        # 弱注入时标明非事实，可影响语气，但不要当经历说出口
        return f"（独处感想，可影响语气）{annotated}"
    return annotated


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


def _resolve_recall_persona(memory_store: MemoryStore, query: str) -> PersonaState:
    mood_content = ""
    try:
        mood_state = memory_store.mood_state()
        if isinstance(mood_state, dict):
            mood_content = str(mood_state.get("content") or "").strip()
    except Exception:  # noqa: BLE001
        mood_content = ""
    return resolve_persona_state(dialogue_text=query, mood_content=mood_content)


def _select_memories(
    memories: list[Any],
    threshold: float,
    limit: int,
    *,
    now: datetime | None = None,
    persona: PersonaState | None = None,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    now = now or datetime.now().astimezone()
    for raw in memories:
        if not isinstance(raw, dict):
            continue
        content = str(raw.get("content") or raw.get("memory") or "").strip()
        if not content:
            continue
        dedupe_key = " ".join(content.lower().split())
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        is_reflection = memory_record_is_reflection(raw) or memory_record_is_reflection(
            {"metadata": metadata, "source": raw.get("source"), "category": raw.get("category")}
        )
        if dedupe_key in seen or _is_memory_expired(raw, metadata, now):
            continue
        score = _optional_score(raw.get("score"))
        if score is not None and score < threshold:
            continue
        source = str(raw.get("source") or metadata.get("source") or "inferred").strip().lower()
        updated_at = str(raw.get("updated_at") or metadata.get("updated_at") or "").strip()
        created_at = str(raw.get("created_at") or metadata.get("created_at") or "").strip()
        importance = _extract_importance(metadata, source)
        memory_kind = str(metadata.get("memory_kind") or "").strip().lower()
        # 回忆强化：最近被 search 过的记忆，用访问时间代替更新时间算衰减
        memory_id = str(raw.get("id") or raw.get("memory_id") or "").strip()
        last_accessed = _get_last_accessed(memory_id)
        effective_ts = last_accessed or updated_at or created_at
        days = _days_since(effective_ts, now)
        decay_weight = _compute_decay_weight(importance, days)
        # volatile 并非"易变应淡出"——它是 expire_memory 设的"临别提权"标记。
        # 即将过期的记忆在消失前需要最后一次露脸机会，让 LLM 能基于它产生替代记忆。
        # 因此这里对 volatile 记忆给予 12% 权重加成，而非衰减。
        if metadata.get("volatile") is True:
            decay_weight = min(1.0, decay_weight * 1.12)
        decay_weight = min(
            1.0,
            decay_weight * _compute_cold_archive_factor(importance, days, memory_kind),
        )
        memory_emotion = str(metadata.get("emotion") or "").strip()
        if persona is not None and memory_emotion:
            decay_weight = min(
                1.0,
                decay_weight * emotion_congruence_factor(persona.active_emotion(), memory_emotion),
            )
        if is_reflection:
            decay_weight *= REFLECTION_AUTO_RECALL_SCORE_FACTOR
        normalized.append(
            {
                "id": memory_id,
                "content": content,
                "score": score,
                "source": source,
                "updated_at": updated_at,
                "created_at": created_at,
                "event_time": str(metadata.get("event_time") or "").strip(),
                "valid_until": str(
                    raw.get("valid_until") or metadata.get("valid_until") or ""
                ).strip(),
                "metadata": metadata,
                "importance": importance,
                "decay_weight": decay_weight,
                "is_reflection": is_reflection,
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
    selected: list[dict[str, Any]] = []
    reflection_count = 0
    for item in normalized:
        if item.get("is_reflection"):
            if reflection_count >= MAX_REFLECTION_IN_AUTO_RECALL:
                continue
            reflection_count += 1
        selected.append(item)
        if len(selected) >= limit:
            break
    return selected


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


def _compute_cold_archive_factor(importance: float, idle_days: float, memory_kind: str) -> float:
    """琐碎共同经历久未激活时额外降权；约定/情感转折豁免。"""
    if memory_kind in EXEMPT_COLD_ARCHIVE_KINDS:
        return 1.0
    if idle_days < COLD_ARCHIVE_IDLE_DAYS or importance >= COLD_ARCHIVE_IMPORTANCE_MAX:
        return 1.0
    excess_days = idle_days - COLD_ARCHIVE_IDLE_DAYS
    fade = min(1.0, excess_days / 120.0) * (COLD_ARCHIVE_IMPORTANCE_MAX - importance)
    return max(COLD_ARCHIVE_DECAY_FLOOR, 1.0 - fade)


def _select_due_commitment_memories(
    memory_store: MemoryStore,
    *,
    now: datetime,
    limit: int = MAX_DUE_COMMITMENT_RECALLS,
) -> list[dict[str, Any]]:
    """扫描约定/纪念日，今天或明天到点的条目主动浮现。"""
    try:
        scope_memories = memory_store.list_scope_memories(limit=200, wait=False)
    except Exception:  # noqa: BLE001
        return []
    due: list[dict[str, Any]] = []
    for raw in scope_memories:
        if not isinstance(raw, dict):
            continue
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        memory_kind = str(metadata.get("memory_kind") or "").strip().lower()
        if memory_kind != "commitment":
            continue
        event_time = str(metadata.get("event_time") or raw.get("event_time") or "").strip()
        if not event_time or not _commitment_is_due(event_time, now):
            continue
        content = str(raw.get("content") or raw.get("memory") or "").strip()
        if not content:
            continue
        source = str(raw.get("source") or metadata.get("source") or "inferred").strip().lower()
        updated_at = str(raw.get("updated_at") or metadata.get("updated_at") or "").strip()
        due.append(
            {
                "id": str(raw.get("id") or raw.get("memory_id") or "").strip(),
                "content": content,
                "score": 0.72,
                "source": source,
                "updated_at": updated_at,
                "created_at": str(raw.get("created_at") or metadata.get("created_at") or "").strip(),
                "event_time": event_time,
                "valid_until": str(
                    raw.get("valid_until") or metadata.get("valid_until") or ""
                ).strip(),
                "metadata": metadata,
                "importance": _extract_importance(metadata, source),
                "decay_weight": DUE_COMMITMENT_DECAY_BOOST,
            }
        )
    due.sort(key=lambda item: (item["updated_at"], item["content"]))
    return due[: max(0, limit)]


def _merge_due_commitments(
    selected: list[dict[str, Any]],
    due_commitments: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    if not due_commitments:
        return selected
    seen_ids = {item["id"] for item in selected if item.get("id")}
    seen_content = {" ".join(item["content"].lower().split()) for item in selected}
    merged = list(selected)
    for commitment in due_commitments:
        memory_id = commitment.get("id") or ""
        dedupe_key = " ".join(commitment["content"].lower().split())
        if memory_id and memory_id in seen_ids:
            continue
        if dedupe_key in seen_content:
            continue
        merged.insert(0, commitment)
        if memory_id:
            seen_ids.add(memory_id)
        seen_content.add(dedupe_key)
        if len(merged) > limit:
            merged.pop()
    return merged[:limit]


def _commitment_is_due(event_time: str, now: datetime) -> bool:
    event_date = _parse_event_date(event_time, now)
    if event_date is None:
        return False
    today = now.date()
    return event_date == today or event_date == today + timedelta(days=1)


def _parse_event_date(event_time: str, now: datetime) -> date | None:
    text = event_time.strip()
    if not text:
        return None
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=now.tzinfo)
            return parsed.date()
        except ValueError:
            pass
        try:
            return date.fromisoformat(candidate[:10])
        except ValueError:
            continue
    return None


def _is_memory_expired(raw: dict[str, Any], metadata: dict[str, Any], now: datetime) -> bool:
    if _is_expired(raw.get("expires_at"), now):
        return True
    return _is_expired(metadata.get("valid_until"), now)


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

def _expand_by_entities(
    memories: list[dict[str, Any]],
    memory_store: Any,
    threshold: float,
    limit: int,
) -> list[dict[str, Any]]:
    """从已检索记忆中提取专有名词，追加相关记忆（多跳召回）。

    例如：搜「最好的朋友」→ 命中「ソフィア是挚友」→
    提取「ソフィア」→ 追加搜索 → 命中「ソフィア会帮我翻译」。
    """
    import re
    entities: set[str] = set()
    # 日文/中文专有名词模式：片假名连续、汉字名（2-4字）、英文名
    entity_patterns = [
        r"[\u30a0-\u30ff]{2,}",           # 片假名（ソフィア、カシマ）
        r"[\u4e00-\u9fff]{2,4}(?:くん|さん|ちゃん|先生|先輩)?",  # 汉字名+敬称
        r"[A-Z][a-z]+(?:\s[A-Z][a-z]+)?",  # 英文名
    ]
    for mem in memories[:5]:  # 只从前5条中提取
        content = str(mem.get("content") or "")
        for pat in entity_patterns:
            for match in re.finditer(pat, content):
                entity = match.group().rstrip("くんさんちゃん先生先輩")
                if len(entity) >= 2 and entity not in ("私", "僕", "俺", "彼", "彼女"):
                    entities.add(entity)

    if not entities:
        return []

    # 用提取到的实体做第二轮搜索
    entity_query = " ".join(sorted(entities)[:3])  # 最多3个实体
    try:
        response = memory_store.search_memory(
            {"query": entity_query, "limit": limit},
            wait=False,
        )
    except Exception:
        return []
    extra = response.get("memories", [])
    if not isinstance(extra, list):
        return []
    # 过滤掉已经有的和低分的
    existing_ids = {str(m.get("id", "")) for m in memories}
    result = []
    for m in extra:
        if str(m.get("id", "")) in existing_ids:
            continue
        score = _bounded_float(m.get("score"), default=0.0)
        if score >= threshold:
            result.append(m)
    return result


def _deduplicate_memories(
    memories: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for m in memories:
        mid = str(m.get("id", ""))
        if mid and mid not in seen:
            seen.add(mid)
            result.append(m)
        elif not mid:
            result.append(m)
    return result
