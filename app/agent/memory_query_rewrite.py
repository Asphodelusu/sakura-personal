"""自动召回用的记忆检索 query 规划。

旧逻辑把「当前输入 + 最近两条用户话 + 视觉摘要」直接拼接，多话题噪声大。
这里优先抽出单句检索意图与实体；有 chat_fast 时再尝试 LLM 改写，失败则回退启发式。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from app.agent.entity_index import (
    extract_entities,
    find_known_entity_aliases,
    is_known_entity_alias,
)
from app.llm.prompts.types import ContextMessage, ContextRequest


MAX_MEMORY_QUERY_CHARS = 4000
MAX_REWRITE_ENTITIES = 8
_REFERENTIAL_RE = re.compile(
    r"(他|她|它|这事|那事|那个|这个|上次|之前|刚才|还记得|记不记得|怎么样了|后来呢)"
)
_TITLE_RE = re.compile(r"《([^》]{1,40})》")
_QUALITY_ENTITY_RE = re.compile(r"[\u30a0-\u30ffA-Za-z]")
_ENTITY_STOPWORDS = frozenset(
    {
        "对方",
        "最近",
        "有没有",
        "怎么样",
        "什么",
        "一个",
        "我们",
        "今天",
        "明天",
        "周末",
        "早上",
        "晚上",
        "后来",
        "清楚",
        "提到",
        "再提",
        "是不是",
        "继续",
        "还是",
    }
)


class _RawCompletionClient(Protocol):
    def complete_raw(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.8,
        *,
        request_timeout: float | None = None,
        max_attempts: int | None = None,
        **chat_params: Any,
    ) -> str: ...


@dataclass(frozen=True)
class MemoryQueryPlan:
    query: str
    entities: tuple[str, ...] = ()
    source: str = "heuristic"  # baseline | heuristic | llm


def build_baseline_memory_query(request: ContextRequest) -> str:
    """旧版拼接：当前输入 + 最近两条用户话 + 视觉摘要。"""
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


def rewrite_memory_query_heuristic(request: ContextRequest) -> MemoryQueryPlan:
    """无模型时的轻量改写：以本轮输入为主，必要时补一条指代上下文与实体。"""
    current = request.current_input.strip()
    recent_user = [
        message.content.strip()
        for message in request.recent_messages
        if message.role == "user" and message.content.strip()
    ]
    # 去掉与 current 完全相同的尾条，避免重复。
    recent_user = [text for text in recent_user if text != current]

    parts: list[str] = []
    if current:
        parts.append(current)
    elif recent_user:
        parts.append(recent_user[-1])

    needs_context = bool(current) and (
        len(current) <= 10 or bool(_REFERENTIAL_RE.search(current))
    )
    if needs_context and recent_user:
        parts.append(recent_user[-1])

    entity_source = "\n".join(parts)
    entities = _select_query_entities(entity_source)
    # 静态别名表层（如「索菲」）优先保留，避免中文正则吞成「索菲是你」
    known = tuple(
        name
        for name in sorted(find_known_entity_aliases(entity_source), key=len, reverse=True)
        if name not in entities
    )
    if known:
        entities = (entities + known)[:MAX_REWRITE_ENTITIES]
    if entities:
        parts.append("关键实体：" + "、".join(entities))

    query = "\n".join(dict.fromkeys(part for part in parts if part)).strip()
    return MemoryQueryPlan(
        query=query[:MAX_MEMORY_QUERY_CHARS].rstrip(),
        entities=entities,
        source="heuristic",
    )


def rewrite_memory_query_llm(
    request: ContextRequest,
    client: _RawCompletionClient,
    *,
    request_timeout: float = 4.0,
) -> MemoryQueryPlan | None:
    """用 flash/chat_fast 抽出单句检索意图；失败返回 None。"""
    current = request.current_input.strip()
    if not current:
        return None
    recent_user = [
        message.content.strip()
        for message in request.recent_messages
        if message.role == "user" and message.content.strip()
    ][-2:]
    payload = {
        "current_input": current,
        "recent_user_messages": recent_user,
        # 视觉摘要只给模型参考，默认不要写进 query
        "visual_summaries": [s.strip() for s in request.visual_summaries if s.strip()][:2],
    }
    system_prompt = (
        "你是记忆检索 query 改写器。根据当前用户输入，产出一句短检索意图（中文为主，可保留作品原名），"
        "并列出关键实体（人名/作品名/专有名词）。\n"
        "规则：\n"
        "1. query 只表达本轮要回忆的主题，不要混入无关闲聊或屏幕摘要。\n"
        "2. 若当前句有指代（他/那个/上次），可借助 recent_user_messages 补全指代对象。\n"
        "3. 不要编造输入里没有的事实。\n"
        '只输出 JSON：{"query":"...","entities":["..."]}'
    )
    try:
        raw = client.complete_raw(
            system_prompt,
            [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            temperature=0.2,
            request_timeout=request_timeout,
            max_attempts=1,
            max_tokens=120,
            response_format={"type": "json_object"},
            thinking={"type": "disabled"},
        )
    except Exception:
        return None
    return _parse_rewrite_payload(raw)


def rewrite_memory_query(
    request: ContextRequest,
    *,
    client: _RawCompletionClient | None = None,
    prefer_llm: bool = True,
) -> MemoryQueryPlan:
    """规划自动召回 query：优先 LLM，失败或不配置时用启发式。"""
    if prefer_llm and client is not None:
        planned = rewrite_memory_query_llm(request, client)
        if planned is not None and planned.query.strip():
            return planned
    if not request.current_input.strip() and not any(
        m.role == "user" and m.content.strip() for m in request.recent_messages
    ):
        baseline = build_baseline_memory_query(request)
        return MemoryQueryPlan(query=baseline, source="baseline")
    return rewrite_memory_query_heuristic(request)


def _parse_rewrite_payload(raw: str) -> MemoryQueryPlan | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 容错：截取首个 JSON 对象
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    query = str(data.get("query") or "").strip()
    if not query:
        return None
    entities_raw = data.get("entities") or []
    entities: list[str] = []
    if isinstance(entities_raw, list):
        for item in entities_raw:
            name = str(item or "").strip()
            if name and name not in entities:
                entities.append(name)
            if len(entities) >= MAX_REWRITE_ENTITIES:
                break
    if entities:
        query = f"{query}\n关键实体：" + "、".join(entities)
    return MemoryQueryPlan(
        query=query[:MAX_MEMORY_QUERY_CHARS].rstrip(),
        entities=tuple(entities),
        source="llm",
    )


def _select_query_entities(text: str) -> tuple[str, ...]:
    """只保留较像专名的实体，避免把口语碎片写进 query 污染检索。"""
    ordered: list[str] = []
    for title in _TITLE_RE.findall(text or ""):
        name = title.strip()
        if name and name not in ordered:
            ordered.append(name)
    for name in sorted(extract_entities(text), key=len, reverse=True):
        clean = name.strip()
        if not clean or clean in _ENTITY_STOPWORDS:
            continue
        # 片假名/英文、较长汉字专名，或静态别名表中的二字中文名（如「索菲」）
        if not (
            _QUALITY_ENTITY_RE.search(clean)
            or len(clean) >= 3
            or is_known_entity_alias(clean)
        ):
            continue
        if clean not in ordered:
            ordered.append(clean)
        if len(ordered) >= MAX_REWRITE_ENTITIES:
            break
    return tuple(ordered[:MAX_REWRITE_ENTITIES])


def context_request_from_parts(
    *,
    current_input: str,
    recent_user_messages: list[str] | None = None,
    visual_summaries: list[str] | None = None,
) -> ContextRequest:
    """测试 / eval 用的轻量构造。"""
    recent = tuple(
        ContextMessage(role="user", content=text)
        for text in (recent_user_messages or [])
        if str(text).strip()
    )
    return ContextRequest(
        current_input=current_input,
        recent_messages=recent,
        visual_summaries=tuple(s for s in (visual_summaries or []) if str(s).strip()),
    )
