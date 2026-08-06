from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.agent.memory import (
    MemoryStore,
    memory_kind_of,
    memory_record_is_meta_reflection,
    memory_record_is_reflection,
    _bounded_float,
)
from app.agent.memory_query_rewrite import (
    MemoryQueryPlan,
    rewrite_memory_query,
)
from app.agent.memory_rerank import rerank_memory_candidates
from app.agent.persona_state import (
    PersonaState,
    emotion_congruence_factor,
    resolve_persona_state,
)
from app.llm.prompts.types import ContextFragment, ContextRequest


DEFAULT_MEMORY_RECALL_LIMIT = 5
# 与改写后的更干净 query 配合：候选略放大，再由阈值+软因子压到 limit。
DEFAULT_MEMORY_RECALL_CANDIDATES = 30
# bge-m3 / 前代中文 bge 相关命中常见在 0.3+；过严会让自动召回空转。
DEFAULT_MEMORY_RELEVANCE_THRESHOLD = 0.3

# 用户在问旧事时，才允许把已失效记忆召回进上下文（默认只取「当前为真」）。
_PAST_LOOKING_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(上次|昨天|前天|之前|早些时候|刚才我们|还记得|记不记得|记得吗|"
        r"以前|从前|那次|那回|那段时间|旧事|当时|那会儿|约定过|约好过|说过吗)"
    ),
)
# 时间衰减半衰期（天），≈ln(2)/λ。λ=0.1 时半衰期约 7 天。
DEFAULT_MEMORY_DECAY_LAMBDA = 0.1
# source=explicit（用户明确要求记住）的记忆自动提至此 importance
EXPLICIT_MEMORY_IMPORTANCE = 0.95
# 回忆强化：被 memory_search 命中后衰减计时重置
# SQLite 持久化实现见 app/agent/access_tracker.py；这里只保留一个懒加载的
# 单例（同一 base_dir 只需要一个 AccessTracker 实例），初始化本身不涉及锁竞争
# 热路径，用简单的模块级锁保护"要不要新建实例"这一步就够了。
_ACCESS_TRACKER_INIT_LOCK = threading.Lock()
# 按 base_dir 缓存 AccessTracker，支持多角色/多数据目录切换。
# 之前用单一全局变量只记第一个 base_dir，切目录后返回错误 tracker。
_ACCESS_TRACKERS: dict[str, Any] = {}
# 约定/纪念日：event_time 落在今天或明天时主动浮现（不占满检索名额）
MAX_DUE_COMMITMENT_RECALLS = 2
DUE_COMMITMENT_DECAY_BOOST = 1.28
# 冷归档：久未激活且低重要性的琐碎记忆缓慢降权（豁免约定/情感转折）
COLD_ARCHIVE_IDLE_DAYS = 45
COLD_ARCHIVE_IMPORTANCE_MAX = 0.38
COLD_ARCHIVE_DECAY_FLOOR = 0.52
EXEMPT_COLD_ARCHIVE_KINDS = frozenset({"commitment", "emotional_turn", "core_profile"})
# 独处反思：自动召回降权弱注入（非硬过滤），每轮最多 1 条，避免抢事实位。
# 这是刻意的策略性硬降权（"感想不能顶事实"），因此独立于下面的软因子加权，
# 不参与 _combine_soft_factors 的连续混合。
REFLECTION_AUTO_RECALL_SCORE_FACTOR = 0.3
MAX_REFLECTION_IN_AUTO_RECALL = 1
# 二阶元反思（对多条一阶反思的再提炼）比一次性感想更接近稳定认知，
# 降权比一阶反思轻很多，且有独立的每轮上限、不与一阶反思共享名额。
META_REFLECTION_AUTO_RECALL_SCORE_FACTOR = 0.75
MAX_META_REFLECTION_IN_AUTO_RECALL = 1

# 软修正因子的合成权重：冷归档 / 情绪一致性 / 临别提权都是"连续、温和"的修正，
# 用 1 + Σ w_i*(factor_i - 1) 的加权效应量相加合成一个乘数，而不是逐个连乘。
# 连乘（f1*f2*f3）会让多个温和的同向偏离互相放大（如 0.85*0.9*0.95≈0.73，
# 比任何单个因子都更极端）；加权效应量相加则只保留每个因子"自己那部分"贡献，
# 总偏离幅度不会明显超过其中最极端的那个因子。权重之和不要求恰好为 1，
# 但保持在 1 附近可以让"多个因子同时轻微利好/不利"时的整体幅度符合直觉。
_COLD_ARCHIVE_FACTOR_WEIGHT = 0.55
_EMOTION_CONGRUENCE_FACTOR_WEIGHT = 0.35
_VOLATILE_BOOST_FACTOR_WEIGHT = 0.55


def _get_access_tracker(base_dir: Path | None) -> Any | None:
    """懒加载 base_dir 对应的 AccessTracker；不同目录各自缓存，失败时退化为不追踪。"""
    if base_dir is None:
        return None
    key = str(base_dir)
    cached = _ACCESS_TRACKERS.get(key)
    if cached is not None:
        return cached
    with _ACCESS_TRACKER_INIT_LOCK:
        if key not in _ACCESS_TRACKERS:
            from app.agent.access_tracker import AccessTracker
            from app.storage.paths import StoragePaths

            try:
                _ACCESS_TRACKERS[key] = AccessTracker(
                    StoragePaths(base_dir).memory_access_tracker_db()
                )
            except Exception:
                _ACCESS_TRACKERS[key] = None
        return _ACCESS_TRACKERS[key]


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
        query_rewriter_client: Any | None = None,
    ) -> None:
        self.memory = memory
        self.limit = max(1, limit)
        self.threshold = threshold
        self._query_rewriter_client = query_rewriter_client

    def set_query_rewriter_client(self, client: Any | None) -> None:
        """注入 chat_fast 等轻量客户端，供自动召回 query 改写。"""
        self._query_rewriter_client = client

    def recall(self, request: ContextRequest, *, light_mode: bool = False) -> MemoryRecallResult:
        plan = self._plan_query(request)
        query = plan.query
        if not query:
            return MemoryRecallResult(query="")

        access_tracker = _get_access_tracker(self.memory.base_dir)

        limit = DEFAULT_MEMORY_RECALL_CANDIDATES
        if light_mode:
            limit = 3  # 轻量模式只取少量候选

        include_expired = _query_looks_past(query)
        try:
            response = self.memory.search_memory(
                {
                    "query": query,
                    "limit": limit,
                    "include_expired": include_expired,
                    # 实体扩展后再统一 rerank，避免对同一批候选打两遍分。
                    "skip_rerank": True,
                },
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
        # 查询侧实体点查：用户说「索菲」时直接按别名捞旧「ソフィア」记忆，不依赖首轮向量命中
        query_entity_hits = _expand_by_query_entities(
            plan,
            request.current_input,
            memories,
            self.memory,
            self.threshold,
            self.limit,
            include_expired=include_expired,
        )
        if query_entity_hits:
            memories = _deduplicate_memories(memories + query_entity_hits)
        # 实体扩展：从首轮结果中提取专有名词，追加相关记忆
        expanded = _expand_by_entities(
            memories,
            self.memory,
            self.threshold,
            self.limit,
            include_expired=include_expired,
        )
        if expanded:
            memories = _deduplicate_memories(memories + expanded)

        # 向量粗捞之后用 cross-encoder 精排；查询侧实体命中随后保底抬回。
        memories = rerank_memory_candidates(
            query,
            memories if isinstance(memories, list) else [],
            base_dir=self.memory.base_dir,
        )
        memories = _apply_entity_query_match_score_floor(
            memories if isinstance(memories, list) else []
        )

        # 回忆强化：只批量点查本轮候选（通常 <=15 条），不管历史累计追踪了多少条，
        # 与旧的"整份 JSON 读入内存"相比，开销只与本轮候选数成正比。
        last_accessed_map: dict[str, str] = {}
        if access_tracker is not None:
            candidate_ids = [str(m.get("id") or "").strip() for m in memories]
            candidate_ids = [mid for mid in candidate_ids if mid]
            if candidate_ids:
                last_accessed_map = access_tracker.get_last_accessed_bulk(candidate_ids)

        select_limit = 2 if light_mode else self.limit
        selected = _select_memories(
            memories,
            self.threshold,
            select_limit,
            now=now,
            persona=persona,
            last_accessed_map=last_accessed_map,
            include_expired=include_expired,
        )
        selected = _merge_due_commitments(selected, due_commitments, self.limit)

        # 回忆强化：记录被选中的记忆（被检索到即算访问），只 UPSERT 这几条
        hit_ids = [m["id"] for m in selected if m.get("id")]
        if access_tracker is not None and hit_ids:
            access_tracker.record_accessed(hit_ids, when=now.isoformat())

        fragments = tuple(
            ContextFragment(
                fragment_id=f"memory.{memory['id'] or index}",
                source="memory",
                # 不再逐条加「与本轮相关的长期记忆：」前缀——source=memory 属性已表明
                # 来源，让每条都带同样的开场白只会显得像检索结果堆砌，也白费 token。
                content=_annotate_recalled_memory_content(memory, now=now),
                trust="trusted" if memory["source"] == "explicit" else "untrusted",
                priority=_recall_fragment_priority(memory),
                freshness=memory["updated_at"],
                token_budget=_recall_fragment_token_budget(memory),
                sensitivity="private",
                cache_scope="turn",
            )
            for index, memory in enumerate(selected)
        )
        return MemoryRecallResult(fragments=fragments, status="ready", query=query)

    def _plan_query(self, request: ContextRequest) -> MemoryQueryPlan:
        return rewrite_memory_query(request, client=self._query_rewriter_client)


def _annotate_recalled_memory_content(memory: dict[str, Any], *, now: datetime) -> str:
    from app.agent.memory import memory_record_is_expired
    from app.agent.time_awareness import annotate_with_relative_age, memory_event_timestamp

    content = str(memory.get("content") or "").strip()
    expired = memory_record_is_expired(memory, now=now)
    kind = memory_kind_of(memory)
    if expired and kind == "commitment":
        expired_label = "已过期的约定"
    elif expired and kind == "recent_status":
        expired_label = "已失效的近况"
    else:
        expired_label = "已失效"
    annotated = annotate_with_relative_age(
        content,
        memory_event_timestamp(memory),
        now=now,
        expired=expired,
        expired_label=expired_label,
    )
    if memory.get("is_meta_reflection"):
        # 二阶元反思：多条独处感想沉淀出的稳定认知，比一次性感想更接近"性格"，
        # 但仍不是可复述的具体经历，标注区别于一阶反思。
        return f"（长期认知，非具体经历）{annotated}"
    if memory_record_is_reflection(memory):
        # 弱注入时标明非事实，可影响语气，但不要当经历说出口
        return f"（独处感想，可影响语气）{annotated}"
    return annotated


def _recall_fragment_priority(memory: dict[str, Any]) -> int:
    if memory.get("is_meta_reflection"):
        return 55
    if memory.get("is_reflection"):
        return 45
    return 80 if memory["source"] == "explicit" else 70


def _recall_fragment_token_budget(memory: dict[str, Any]) -> int:
    if memory.get("is_meta_reflection"):
        return 320
    if memory.get("is_reflection"):
        return 280
    return 512


def _build_memory_query(request: ContextRequest) -> str:
    """兼容旧调用：返回改写后的自动召回 query。"""
    return rewrite_memory_query(request, client=None).query


def _resolve_recall_persona(memory_store: MemoryStore, query: str) -> PersonaState:
    mood_content = ""
    try:
        mood_state = memory_store.mood_state()
        if isinstance(mood_state, dict):
            mood_content = str(mood_state.get("content") or "").strip()
    except Exception:  # noqa: BLE001
        mood_content = ""
    reply_emotion = ""
    try:
        reply_emotion = str(memory_store.sakura_reply_emotion() or "").strip()
    except Exception:  # noqa: BLE001
        reply_emotion = ""
    return resolve_persona_state(
        dialogue_text=query,
        mood_content=mood_content,
        sakura_reply_emotion=reply_emotion,
    )


def _query_looks_past(query: str) -> bool:
    """用户是否在显式追问旧事/过往约定——此时才放行已失效记忆。"""
    text = (query or "").strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _PAST_LOOKING_PATTERNS)


def _select_memories(
    memories: list[Any],
    threshold: float,
    limit: int,
    *,
    now: datetime | None = None,
    persona: PersonaState | None = None,
    last_accessed_map: dict[str, str] | None = None,
    include_expired: bool = False,
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
        is_meta_reflection = memory_record_is_meta_reflection(raw) or memory_record_is_meta_reflection(
            {"metadata": metadata, "category": raw.get("category")}
        )
        if dedupe_key in seen:
            continue
        if not include_expired and _is_memory_expired(raw, metadata, now):
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
        last_accessed = (last_accessed_map or {}).get(memory_id, "")
        effective_ts = last_accessed or updated_at or created_at
        days = _days_since(effective_ts, now)
        decay_weight = _compute_decay_weight(importance, days)
        # volatile 并非"易变应淡出"——它是 expire_memory 设的"临别提权"标记。
        # 即将过期的记忆在消失前需要最后一次露脸机会，让 LLM 能基于它产生替代记忆。
        # 因此这里对 volatile 记忆给予权重加成，而非衰减。
        volatile_factor = 1.12 if metadata.get("volatile") is True else 1.0
        cold_archive_factor = _compute_cold_archive_factor(importance, days, memory_kind)
        memory_emotion = str(metadata.get("emotion") or "").strip()
        emotion_factor = 1.0
        if persona is not None and memory_emotion:
            emotion_factor = emotion_congruence_factor(persona.active_emotion(), memory_emotion)
        # 三个"软"因子按加权效应量合成，避免连乘时互相放大（见函数说明）。
        soft_multiplier = _combine_soft_factors(
            (cold_archive_factor, _COLD_ARCHIVE_FACTOR_WEIGHT),
            (emotion_factor, _EMOTION_CONGRUENCE_FACTOR_WEIGHT),
            (volatile_factor, _VOLATILE_BOOST_FACTOR_WEIGHT),
        )
        decay_weight = min(1.0, max(0.0, decay_weight * soft_multiplier))
        # 反思类的降权是策略性硬门（"感想不能顶事实"），不是几何/时间修正，
        # 因此始终保持独立的显式连乘，不纳入上面的软因子合成。
        if is_meta_reflection:
            decay_weight *= META_REFLECTION_AUTO_RECALL_SCORE_FACTOR
        elif is_reflection:
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
                "is_meta_reflection": is_meta_reflection,
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
    meta_reflection_count = 0
    for item in normalized:
        if item.get("is_meta_reflection"):
            if meta_reflection_count >= MAX_META_REFLECTION_IN_AUTO_RECALL:
                continue
            meta_reflection_count += 1
        elif item.get("is_reflection"):
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


def _combine_soft_factors(*factors_with_weights: tuple[float, float]) -> float:
    """把多个"软"修正因子（各自以 1.0 为中性值）按加权效应量合成一个乘数。

    见模块顶部关于 _COLD_ARCHIVE_FACTOR_WEIGHT 等权重的说明：这里用
    1 + Σ w_i*(f_i - 1) 而非连乘，避免多个温和的同向偏离互相放大。
    结果裁剪到 [0, ∞)，调用方仍需自行 clamp 到期望的上限（如 1.0）。
    """
    total = 1.0
    for factor, weight in factors_with_weights:
        total += weight * (factor - 1.0)
    return max(0.0, total)


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
    from app.agent.memory import (
        commitment_event_time,
        memory_kind_of,
        sweep_stale_commitments,
    )

    # 先关掉已过期日的约定，避免语义检索仍把旧约定当现行事实捞起。
    try:
        sweep_stale_commitments(memory_store, now=now)
    except Exception:  # noqa: BLE001
        pass
    try:
        scope_memories = memory_store.list_scope_memories(limit=200, wait=False)
    except Exception:  # noqa: BLE001
        return []
    due: list[dict[str, Any]] = []
    for raw in scope_memories:
        if not isinstance(raw, dict):
            continue
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        if memory_kind_of(raw) != "commitment":
            continue
        event_time = commitment_event_time(raw)
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
    from app.agent.time_awareness import parse_memory_event_date

    event_date = parse_memory_event_date(event_time, now=now)
    if event_date is None:
        return False
    today = now.date()
    return event_date == today or event_date == today + timedelta(days=1)


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

# 从实体索引点查到的记忆没有语义相似度分数（不是靠向量搜索命中的）。
# 查询侧点查（用户点名「索菲」）给高合成分，避免被近期中文情景记忆压掉；
# 多跳 hop 仍用中性分，只作补充线索。
_ENTITY_HOP_SYNTHETIC_SCORE = 0.55
_ENTITY_QUERY_MATCH_SCORE = 0.82
# 精排可能把「中文问句 × 日文设定」打低；查询侧实体命中保底，避免又掉出名单。
_ENTITY_QUERY_MATCH_RERANK_FLOOR = 0.75


def _memories_for_entity_names(
    entities: set[str],
    memories: list[dict[str, Any]],
    memory_store: Any,
    threshold: float,
    limit: int,
    *,
    include_expired: bool = False,
    query_match: bool = False,
    wait: bool = False,
) -> list[dict[str, Any]]:
    """按实体名点查索引并取回全文，合成分给未带 score 的条目。"""
    if not entities:
        return []
    existing_ids = {str(m.get("id", "")) for m in memories}
    try:
        hop_ids = memory_store.lookup_entity_memory_ids(
            entities, exclude_ids=existing_ids, limit=limit
        )
    except Exception:
        return []
    if not hop_ids:
        return []

    try:
        response = memory_store.get_memory_detail(
            {"ids": hop_ids, "include_expired": include_expired},
            wait=wait,
        )
    except Exception:
        return []
    extra = response.get("memories", [])
    if not isinstance(extra, list):
        return []

    synthetic = _ENTITY_QUERY_MATCH_SCORE if query_match else _ENTITY_HOP_SYNTHETIC_SCORE
    result = []
    for m in extra:
        if str(m.get("id", "")) in existing_ids:
            continue
        item = dict(m)
        if item.get("score") is None or query_match:
            item["score"] = max(_bounded_float(item.get("score"), default=0.0), synthetic)
        if query_match:
            item["entity_query_match"] = True
        score = _bounded_float(item.get("score"), default=0.0)
        if score >= threshold:
            result.append(item)
    return result


def _apply_entity_query_match_score_floor(
    memories: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """精排后抬回查询侧实体命中，避免中日别名设定被打没。"""
    if not memories:
        return memories
    raised: list[dict[str, Any]] = []
    changed = False
    for memory in memories:
        if not isinstance(memory, dict):
            continue
        if memory.get("entity_query_match"):
            score = _bounded_float(memory.get("score"), default=0.0)
            if score < _ENTITY_QUERY_MATCH_RERANK_FLOOR:
                memory = {**memory, "score": _ENTITY_QUERY_MATCH_RERANK_FLOOR}
                changed = True
        raised.append(memory)
    if not changed:
        return memories
    raised.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
    return raised


def _expand_by_query_entities(
    plan: MemoryQueryPlan,
    current_input: str,
    memories: list[dict[str, Any]],
    memory_store: Any,
    threshold: float,
    limit: int,
    *,
    include_expired: bool = False,
) -> list[dict[str, Any]]:
    """用本轮 query / 用户原话里的实体直接点查（含中日别名展开）。"""
    from app.agent.entity_index import extract_entities, find_known_entity_aliases

    entities: set[str] = {str(e).strip() for e in plan.entities if str(e).strip()}
    entities |= extract_entities(plan.query or "")
    entities |= extract_entities(current_input or "")
    entities |= find_known_entity_aliases(plan.query or "")
    entities |= find_known_entity_aliases(current_input or "")
    return _memories_for_entity_names(
        entities,
        memories,
        memory_store,
        threshold,
        limit,
        include_expired=include_expired,
        query_match=True,
    )


def _expand_by_entities(
    memories: list[dict[str, Any]],
    memory_store: Any,
    threshold: float,
    limit: int,
    *,
    include_expired: bool = False,
) -> list[dict[str, Any]]:
    """从已检索记忆中提取专有名词，按持久实体索引追加相关记忆（多跳召回）。

    例如：搜「最好的朋友」→ 命中「ソフィア是挚友」→ 提取「ソフィア」→
    查实体索引 → 命中「ソフィア会帮我翻译」这条记忆的 id → 按 id 直接取回全文。

    相比"再发一次语义搜索"：这里的实体→记忆 id 关联在记忆写入/整理时就已经
    持久化好了（见 app/agent/entity_index.py），查询时只是一次本地索引点查
    + 按 id 批量取回，不需要重新计算 embedding，也不会每轮都对同一实体
    重复做一次全量语义搜索。
    """
    from app.agent.entity_index import extract_entities

    entities: set[str] = set()
    for mem in memories[:5]:  # 只从前5条中提取
        entities |= extract_entities(str(mem.get("content") or ""))
    return _memories_for_entity_names(
        entities,
        memories,
        memory_store,
        threshold,
        limit,
        include_expired=include_expired,
        query_match=False,
    )


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
