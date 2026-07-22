"""召回评分：软因子加权效应量相加 + 二阶元反思弱注入。"""

from __future__ import annotations

from app.agent.memory_recall import (
    MAX_META_REFLECTION_IN_AUTO_RECALL,
    MAX_REFLECTION_IN_AUTO_RECALL,
    META_REFLECTION_AUTO_RECALL_SCORE_FACTOR,
    REFLECTION_AUTO_RECALL_SCORE_FACTOR,
    _combine_soft_factors,
    _select_memories,
)


def test_combine_soft_factors_neutral_is_identity() -> None:
    assert _combine_soft_factors((1.0, 0.5), (1.0, 0.3)) == 1.0


def test_combine_soft_factors_does_not_compound_below_worst_factor() -> None:
    """连乘 0.85*0.9=0.765 会比任何单个因子都更极端；加权效应量相加不应该。"""
    combined = _combine_soft_factors((0.85, 0.55), (0.9, 0.35))
    multiplicative = 0.85 * 0.9
    assert combined > multiplicative
    assert combined >= min(0.85, 0.9)


def test_combine_soft_factors_boost_and_penalty_partially_offset() -> None:
    """一个利好因子和一个不利因子同时存在时，效果应互相抵消而不是被不利因子拖垮。"""
    combined = _combine_soft_factors((1.14, 0.35), (0.6, 0.55))
    assert 0.6 < combined < 1.0


def test_meta_reflection_downweighted_less_than_raw_reflection() -> None:
    """二阶元反思是被沉淀过的稳定认知，降权应明显轻于一次性独处感想。"""
    assert META_REFLECTION_AUTO_RECALL_SCORE_FACTOR > REFLECTION_AUTO_RECALL_SCORE_FACTOR


def test_select_memories_separates_reflection_and_meta_reflection_caps() -> None:
    selected = _select_memories(
        [
            {
                "id": "f1",
                "content": "他喜欢抹茶",
                "score": 0.9,
                "source": "self_curation",
                "metadata": {"importance": 0.8},
            },
            {
                "id": "r1",
                "content": "我意识到他最近更依赖我",
                "score": 0.95,
                "source": "reflection",
                "category": "reflection",
                "metadata": {"importance": 0.9, "memory_kind": "reflection"},
            },
            {
                "id": "m1",
                "content": "我发现自己一直在意他有没有按时吃饭，这好像已经成了习惯",
                "score": 0.97,
                "source": "reflection",
                "category": "meta_reflection",
                "metadata": {"importance": 0.85, "memory_kind": "meta_reflection"},
            },
        ],
        threshold=0.3,
        limit=5,
    )
    ids = [m["id"] for m in selected]
    assert "f1" in ids
    # 一阶反思和二阶元反思分别有独立的名额上限，不会因为共享一个名额而互相挤掉
    assert "r1" in ids
    assert "m1" in ids
    reflection_items = [m for m in selected if m.get("is_reflection") and not m.get("is_meta_reflection")]
    meta_items = [m for m in selected if m.get("is_meta_reflection")]
    assert len(reflection_items) <= MAX_REFLECTION_IN_AUTO_RECALL
    assert len(meta_items) <= MAX_META_REFLECTION_IN_AUTO_RECALL


def test_select_memories_uses_last_accessed_map_over_updated_at() -> None:
    """回忆强化：命中过的记忆应该用访问时间而不是更新时间起算衰减。"""
    old_but_recently_accessed = _select_memories(
        [
            {
                "id": "m1",
                "content": "很久以前的一条低重要度记忆",
                "score": 0.9,
                "source": "self_curation",
                "updated_at": "2020-01-01T00:00:00+08:00",
                "metadata": {"importance": 0.1},
            },
        ],
        threshold=0.3,
        limit=5,
        last_accessed_map={"m1": "2026-07-20T00:00:00+08:00"},
    )
    old_and_never_accessed = _select_memories(
        [
            {
                "id": "m1",
                "content": "很久以前的一条低重要度记忆",
                "score": 0.9,
                "source": "self_curation",
                "updated_at": "2020-01-01T00:00:00+08:00",
                "metadata": {"importance": 0.1},
            },
        ],
        threshold=0.3,
        limit=5,
        last_accessed_map=None,
    )
    # 最近被命中过的记忆衰减权重应明显更高（相当于没过期太久）
    assert old_but_recently_accessed[0]["decay_weight"] > old_and_never_accessed[0]["decay_weight"]


def test_meta_reflection_still_ranks_below_facts_with_similar_score() -> None:
    selected = _select_memories(
        [
            {
                "id": "f1",
                "content": "他喜欢抹茶",
                "score": 0.7,
                "source": "self_curation",
                "metadata": {"importance": 0.8},
            },
            {
                "id": "m1",
                "content": "我好像一直很在意他吃没吃饭",
                "score": 0.7,
                "source": "reflection",
                "category": "meta_reflection",
                "metadata": {"importance": 0.8, "memory_kind": "meta_reflection"},
            },
        ],
        threshold=0.3,
        limit=5,
    )
    ids = [m["id"] for m in selected]
    assert ids[0] == "f1"
