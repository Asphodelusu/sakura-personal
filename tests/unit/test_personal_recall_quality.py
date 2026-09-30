"""Personal recall keeps the Qt quality chain: rerank, decay, due commitments."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from plugins.builtin.sakura_mem0.boundary import _project_memory
from plugins.builtin.sakura_mem0.domain_types import ContextRequest
from plugins.builtin.sakura_mem0.memory_recall import MemoryRecallService
from plugins.builtin.sakura_mem0.personal_recall import (
    ACCESS_TRACKER_FILE,
    AccessTracker,
    PersonalRecallPolicy,
)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone(timedelta(hours=8)))


def _memory(key: str, content: str, **fields):
    return {"id": key, "content": content, "source": "inferred", "updatedAt": NOW.isoformat(), **fields}


def _policy(tmp_path: Path, **kwargs) -> PersonalRecallPolicy:
    return PersonalRecallPolicy(tmp_path, clock=lambda: NOW, **kwargs)


class _Reranker:
    def __init__(self, scores):
        self.scores = scores

    def available(self):
        return True

    def score(self, query, texts):
        return [self.scores[text] for text in texts]

    def close(self):
        pass


def test_semantic_only_hits_are_not_halved_by_bm25(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    selected = policy.select("q", [
        _memory("a", "semantic only", score=0.2, semanticScore=0.62),
        _memory("b", "weak", score=0.4, semanticScore=0.25),
    ], 5)
    assert [item["id"] for item in selected] == ["a"]


def test_mem0_exposes_the_semantic_score_through_the_projection(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "plugins/builtin/sakura_mem0"))
    from mem0.utils.scoring import score_and_rank

    scored = score_and_rank(
        [{"id": "m", "score": 0.6, "payload": {"data": "x"}}], {"m": 0.0, "other": 1.0}, {}, 0.1, 5
    )
    assert scored[0]["semantic_score"] == 0.6 and scored[0]["score"] == 0.3
    projected = _project_memory({"id": "m", "memory": "x", "score": 0.3, "semantic_score": 0.6}, "alice")
    assert projected["semanticScore"] == 0.6


def test_rerank_reorders_and_only_drops_clear_misses(tmp_path: Path) -> None:
    policy = _policy(tmp_path, reranker=_Reranker({"first": 0.2, "second": 0.9, "miss": 0.05}))
    selected = policy.select("q", [
        _memory("1", "first", semanticScore=0.9),
        _memory("2", "second", semanticScore=0.35),
        _memory("3", "miss", semanticScore=0.8),
    ], 5)
    assert [item["id"] for item in selected] == ["2", "1"]


def test_old_trivia_decays_while_important_or_explicit_memories_stay(tmp_path: Path) -> None:
    old = (NOW - timedelta(days=60)).isoformat()
    policy = _policy(tmp_path)
    selected = policy.select("q", [
        _memory("trivia", "old trivia", semanticScore=0.8, importance=0.2, updatedAt=old),
        _memory("fresh", "fresh note", semanticScore=0.6, importance=0.2),
        _memory("told", "you told me", semanticScore=0.7, source="explicit", updatedAt=old),
    ], 5)
    assert [item["id"] for item in selected] == ["told", "fresh", "trivia"]


def test_expired_memories_return_only_when_asked_about_the_past(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    memories = [
        _memory("status", "was busy", semanticScore=0.7, validUntil=(NOW - timedelta(days=1)).isoformat(),
                memoryKind="recent_status"),
        _memory("promise", "movie night", semanticScore=0.7, memoryKind="commitment",
                eventTime=(NOW - timedelta(days=2)).date().isoformat()),
    ]
    assert policy.select("今天做什么", memories, 5) == []
    past = policy.select("还记得上次约好的吗", memories, 5)
    assert {item["id"] for item in past} == {"status", "promise"}
    annotated = {item["id"]: policy.annotate(item) for item in past}
    assert annotated["promise"].startswith("（已过期的约定")
    assert annotated["status"].startswith("（已失效的近况")


def test_due_commitments_surface_first(tmp_path: Path) -> None:
    tomorrow = (NOW + timedelta(days=1)).date().isoformat()
    due = {"id": "due", "memory": "anniversary", "metadata": {"memory_kind": "commitment", "event_time": tomorrow}}
    policy = _policy(tmp_path, list_memories=lambda: [due])
    selected = policy.select("q", [_memory("a", "other", semanticScore=0.8)], 2)
    assert [item["id"] for item in selected] == ["due", "a"]


def test_at_most_one_reflection_and_it_is_labelled(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    selected = policy.select("q", [
        _memory("r1", "thought one", semanticScore=0.9, category="reflection", importance=1.0),
        _memory("r2", "thought two", semanticScore=0.9, category="reflection", importance=1.0),
        _memory("m", "stable view", semanticScore=0.9, memoryKind="meta_reflection", importance=1.0),
        _memory("f", "fact", semanticScore=0.5),
    ], 5)
    assert [item["id"] for item in selected] == ["m", "f", "r1"]
    labels = [policy.annotate(item) for item in selected]
    assert labels[0].startswith("（长期认知，非具体经历）")
    assert labels[2].startswith("（独处感想，可影响语气）")


def test_recalled_memories_refresh_their_decay_clock(tmp_path: Path) -> None:
    rehearsal = _policy(tmp_path / "rehearsal")
    (tmp_path / "rehearsal").mkdir()
    rehearsal.select("q", [_memory("a", "x", semanticScore=0.8)], 5)
    assert not (tmp_path / "rehearsal" / ACCESS_TRACKER_FILE).exists()

    daily = _policy(tmp_path, record_access=True)
    old = (NOW - timedelta(days=90)).isoformat()
    daily.select("q", [_memory("a", "x", semanticScore=0.8, importance=0.2, updatedAt=old)], 5)
    daily.close()
    tracker = AccessTracker(tmp_path / ACCESS_TRACKER_FILE)
    try:
        assert tracker.last_accessed(["a"]) == {"a": NOW.isoformat()}
    finally:
        tracker.close()


def test_recall_service_uses_the_policy_when_the_memory_offers_one(tmp_path: Path) -> None:
    class Memory:
        recall_policy = _policy(tmp_path)

        def __init__(self):
            self.limits = []

        def search_memory(self, arguments, *, wait=False):
            self.limits.append(arguments["limit"])
            return {"status": "ready", "memories": [
                _memory("a", "thing", semanticScore=0.8, createdAt=(NOW - timedelta(days=1)).isoformat()),
            ]}

    memory = Memory()
    result = MemoryRecallService(memory).recall(ContextRequest(current_input="thing?", character_id="alice"))
    assert memory.limits == [30]
    assert result.fragments[0].content == "（昨天）thing"
