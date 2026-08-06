"""自动召回：查询侧实体点查（含中日别名）不依赖首轮向量命中。"""

from __future__ import annotations

from typing import Any

from app.agent.memory_query_rewrite import MemoryQueryPlan
from app.agent.memory_recall import (
    MemoryRecallService,
    _ENTITY_QUERY_MATCH_RERANK_FLOOR,
    _ENTITY_QUERY_MATCH_SCORE,
    _apply_entity_query_match_score_floor,
    _expand_by_query_entities,
)
from app.llm.prompts.types import ContextRequest


class _FakeMemoryStore:
    def __init__(self, by_id: dict[str, dict[str, Any]], entity_map: dict[str, list[str]]) -> None:
        self.base_dir = None
        self._by_id = by_id
        self._entity_map = entity_map

    def search_memory(self, arguments: dict[str, Any], *, wait: bool = True) -> dict[str, Any]:
        return {"status": "ready", "memories": []}

    def lookup_entity_memory_ids(
        self,
        entities: Any,
        *,
        exclude_ids: Any = (),
        limit: int = 20,
    ) -> list[str]:
        from app.agent.entity_index import expand_entity_aliases

        exclude = {str(i) for i in exclude_ids}
        hits: list[str] = []
        for name in expand_entity_aliases(entities):
            for mid in self._entity_map.get(name, []):
                if mid not in exclude and mid not in hits:
                    hits.append(mid)
                if len(hits) >= limit:
                    return hits
        return hits

    def get_memory_detail(self, arguments: dict[str, Any], *, wait: bool = True) -> dict[str, Any]:
        ids = [str(i) for i in (arguments.get("ids") or [])]
        return {"memories": [dict(self._by_id[i]) for i in ids if i in self._by_id]}


def test_expand_by_query_entities_uses_chinese_alias() -> None:
    lore = {
        "id": "lore-sophia",
        "content": "ソフィアは学園の仲間で、生徒会の仕事もよく手伝ってくれる。",
        "source": "self_curation",
        "updated_at": "2026-07-15T12:00:00+08:00",
        "metadata": {"importance": 0.9},
    }
    store = _FakeMemoryStore(
        by_id={"lore-sophia": lore},
        entity_map={"ソフィア": ["lore-sophia"]},
    )
    plan = MemoryQueryPlan(query="索菲是不是学生会朋友", entities=("索菲",), source="heuristic")
    hits = _expand_by_query_entities(
        plan,
        "那不是你学生会里的好朋友吗？索菲",
        memories=[],
        memory_store=store,
        threshold=0.3,
        limit=5,
    )
    assert len(hits) == 1
    assert hits[0]["id"] == "lore-sophia"
    assert hits[0]["score"] == _ENTITY_QUERY_MATCH_SCORE
    assert hits[0].get("entity_query_match") is True


def test_entity_query_match_score_floor_survives_rerank_demotion() -> None:
    memories = [
        {"id": "recent", "content": "游戏角色索菲", "score": 0.9},
        {
            "id": "lore-sophia",
            "content": "ソフィアは学園の仲間",
            "score": 0.2,
            "entity_query_match": True,
        },
    ]
    raised = _apply_entity_query_match_score_floor(memories)
    by_id = {m["id"]: m for m in raised}
    assert by_id["lore-sophia"]["score"] == _ENTITY_QUERY_MATCH_RERANK_FLOOR
    assert raised[0]["id"] == "recent" or raised[0]["score"] >= _ENTITY_QUERY_MATCH_RERANK_FLOOR


def test_recall_returns_entity_hit_when_vector_empty(monkeypatch: Any) -> None:
    lore = {
        "id": "lore-sophia",
        "content": "ソフィアは学園の仲間で、私が信頼している数少ない人物の一人。",
        "source": "self_curation",
        "updated_at": "2026-07-15T12:00:00+08:00",
        "metadata": {"importance": 0.9, "layer": "semantic"},
        "layer": "semantic",
        "score": _ENTITY_QUERY_MATCH_SCORE,
        "entity_query_match": True,
    }
    store = _FakeMemoryStore(
        by_id={"lore-sophia": lore},
        entity_map={"ソフィア": ["lore-sophia"]},
    )
    service = MemoryRecallService(store, threshold=0.3, limit=5)

    monkeypatch.setattr(
        "app.agent.memory_recall.rerank_memory_candidates",
        lambda query, memories, base_dir=None: memories,
    )
    monkeypatch.setattr("app.agent.memory_recall._get_access_tracker", lambda _base: None)
    monkeypatch.setattr(
        "app.agent.memory_recall._resolve_recall_persona",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.agent.memory_recall._select_due_commitment_memories",
        lambda *_args, **_kwargs: [],
    )

    result = service.recall(
        ContextRequest(
            current_input="索菲是你学生会里的好朋友吧？",
            recent_messages=(),
            visual_summaries=(),
        )
    )
    assert result.status == "ready"
    assert any("ソフィア" in fragment.content for fragment in result.fragments)
