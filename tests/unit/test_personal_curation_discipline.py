"""The personal curator keeps the Qt discipline: grounded, typed, dated, first person."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from plugins.builtin.sakura_mem0 import personal_curation_discipline as discipline
from plugins.builtin.sakura_mem0.domain_types import ChatHistoryEntry
from plugins.builtin.sakura_mem0.memory_curator import MemoryCurator


class _Store:
    personal_discipline = True
    character_name = "Sakura"
    scope_id = "Sakura"

    def __init__(self, memories=()):
        self.records = {item["id"]: dict(item) for item in memories}
        self.calls = []

    def list_memories(self, *, limit=None):
        return [dict(item, metadata=dict(item.get("metadata") or {})) for item in self.records.values()]

    def create_memory(self, arguments, *, allow_sensitive=False):
        key = f"new-{len(self.records)}"
        metadata = {k: v for k, v in arguments.items() if k not in {"content", "layer", "category"}}
        self.records[key] = {"id": key, "content": arguments["content"], "layer": arguments.get("layer"),
                             "category": arguments.get("category"), "metadata": metadata}
        self.calls.append(("create", dict(arguments)))
        return {"ok": True}

    def update_memory(self, arguments, *, allow_sensitive=False):
        record = self.records[arguments["id"]]
        record["content"] = arguments.get("content", record["content"])
        record.setdefault("metadata", {}).update({k: v for k, v in arguments.items() if k not in {"id", "content"}})
        self.calls.append(("update", dict(arguments)))
        return {"ok": True}

    def delete_memory(self, arguments):
        self.records.pop(arguments["id"], None)
        self.calls.append(("delete", dict(arguments)))
        return {"ok": True}


class _Api:
    def __init__(self, *operation_lists):
        self.responses = [json.dumps({"operations": ops}, ensure_ascii=False) for ops in operation_lists]
        self.prompts = []

    def complete_raw(self, system, messages, **_kwargs):
        self.prompts.append((system, messages[0]["content"]))
        return self.responses.pop(0) if self.responses else json.dumps({"operations": []})


DIALOG = [
    ChatHistoryEntry(created_at="2026-09-30T10:00:00+08:00", role="user", content="明天晚上八点一起看电影吧",
                     entry_id="u1", turn_id="t1"),
    ChatHistoryEntry(created_at="2026-09-30T10:00:05+08:00", role="assistant", content="好呀，那就说定了。",
                     entry_id="a1", turn_id="t1"),
    ChatHistoryEntry(created_at="2026-09-30T10:01:00+08:00", role="user", content="我最近在忙毕业设计，好累",
                     entry_id="u2", turn_id="t2"),
]


def _curate(store, api):
    return MemoryCurator(api, store, system_prompt="FULL PERSONA CARD").curate_entries(DIALOG)


def _created(store):
    return [arguments for kind, arguments in store.calls if kind == "create"]


def test_grounded_typed_writes_keep_their_kind_date_and_lifetime() -> None:
    store = _Store()
    _curate(store, _Api([
        {"op": "add", "content": "我和他约定明天晚上八点一起看电影", "layer": "episodic", "memory_kind": "commitment",
         "event_time": "2026-10-01T20:00:00+08:00", "evidence": "明天晚上八点一起看电影", "emotion": "HAPPY"},
        {"op": "add", "content": "他最近在忙毕业设计", "layer": "semantic", "memory_kind": "recent_status",
         "evidence": "我最近在忙毕业设计"},
    ]))
    commitment, status = _created(store)
    assert commitment["event_time"] == "2026-10-01T20:00:00+08:00" and commitment["emotion"] == "happy"
    assert status["volatile"] is True
    assert datetime.fromisoformat(status["valid_until"]) > datetime.now().astimezone() + timedelta(days=13)
    assert status["evidence"] == "我最近在忙毕业设计"


def test_ungrounded_forged_transient_or_undated_writes_are_refused() -> None:
    store = _Store()
    result = _curate(store, _Api([
        {"op": "add", "content": "他喜欢吃拉面", "layer": "semantic"},
        {"op": "add", "content": "他说想去海边", "layer": "semantic", "evidence": "我想去海边"},
        {"op": "add", "content": "当前本地时间：十点", "layer": "semantic", "evidence": "明天晚上八点"},
        {"op": "add", "content": "我和他约定一起看电影", "layer": "episodic", "memory_kind": "commitment",
         "evidence": "一起看电影"},
        {"op": "add", "content": "Sakura觉得他最近在忙毕业设计", "layer": "semantic", "evidence": "我最近在忙毕业设计"},
        {"op": "add", "content": "好呀", "layer": "semantic", "evidence": "好呀"},
    ]))
    assert _created(store) == []
    counts = result.event_counts
    assert counts["SKIP_UNGROUNDED"] == 2
    assert counts["SKIP_TRANSIENT"] == 1
    assert counts["SKIP_COMMITMENT_NO_EVENT_TIME"] == 1
    assert counts["SKIP_SPEAKER"] == 1
    assert counts["SKIP_TRIVIAL"] == 1


def test_updates_run_before_adds_and_a_completed_promise_is_not_re_added() -> None:
    store = _Store([{"id": "m1", "content": "我答应下次由我主动邀请他看电影", "layer": "episodic",
                     "metadata": {"memory_kind": "commitment", "event_time": (datetime.now().astimezone() + timedelta(days=7)).date().isoformat()}}])
    result = _curate(store, _Api([
        {"op": "add", "content": "我承诺下次由我主动邀请他看电影", "layer": "episodic", "evidence": "一起看电影吧"},
        {"op": "update", "id": "m1", "content": "约定已完成。我主动邀请他明天晚上八点一起看电影",
         "layer": "episodic", "evidence": "明天晚上八点一起看电影"},
    ]))
    assert [kind for kind, _arguments in store.calls] == ["update"]
    assert result.event_counts["SKIP_STALE_COMMITMENT"] == 1


def test_a_new_status_supersedes_a_similar_old_one() -> None:
    store = _Store([{"id": "old", "content": "他最近在忙毕业设计的开题", "layer": "semantic",
                     "metadata": {"memory_kind": "recent_status", "volatile": True}}])
    result = _curate(store, _Api([
        {"op": "add", "content": "他最近在忙毕业设计", "layer": "semantic", "memory_kind": "recent_status",
         "evidence": "我最近在忙毕业设计"},
    ]))
    assert result.event_counts.get("SUPERSEDE_VOLATILE") == 1 or result.event_counts.get("MERGE_UPDATE") == 1
    assert "valid_until" in store.records["old"]["metadata"] or store.records["old"]["content"] == "他最近在忙毕业设计"


def test_past_commitments_close_and_are_reviewed_once() -> None:
    past = (datetime.now().astimezone() - timedelta(days=2)).date().isoformat()
    store = _Store([{"id": "c1", "content": "我和他约好周末去看展", "layer": "episodic",
                     "metadata": {"memory_kind": "commitment", "event_time": past}}])
    api = _Api([], [])
    first = _curate(store, api)
    assert first.event_counts["COMMITMENT_SWEPT"] == 1
    assert first.event_counts["EXPIRY_REVIEWED"] == 1
    assert "【刚过期的约定（一次性回顾）】" in api.prompts[0][1]
    assert store.records["c1"]["metadata"]["expiry_reviewed"] is True
    _curate(store, api)
    assert "刚过期的约定" not in api.prompts[1][1]


def test_personal_prompt_uses_the_discipline_not_the_full_card() -> None:
    store = _Store([
        {"id": "r1", "content": "我注意到他最近总熬夜", "layer": "episodic", "category": "reflection"},
        {"id": "gone", "content": "已放下的旧事", "layer": "semantic", "metadata": {"status": "released"}},
    ])
    api = _Api([])
    _curate(store, api)
    system, user = api.prompts[0]
    assert "FULL PERSONA CARD" not in system
    assert system.startswith("身份锚点：你是「Sakura」")
    assert "证据纪律" in system and "memory_kind=commitment" in system
    assert "(独处感想/非事实)" in user
    assert "已放下的旧事" not in user
    assert "他：明天晚上八点一起看电影吧" in user and "我：好呀" in user
    assert "铭" not in discipline.PERSONAL_CURATION_TASK_PROMPT


def test_superseding_marks_only_similar_volatile_statuses() -> None:
    store = _Store([
        {"id": "old", "content": "他最近在忙毕业设计的开题", "layer": "semantic",
         "metadata": {"memory_kind": "recent_status", "volatile": True}},
        {"id": "other", "content": "他喜欢樱花", "layer": "semantic", "metadata": {"memory_kind": "recent_status"}},
        {"id": "fact", "content": "他最近在忙毕业设计的开题", "layer": "semantic", "metadata": {}},
    ])
    operation = {"content": "他最近在忙毕业设计的答辩", "memory_kind": "recent_status"}
    assert discipline.expire_superseded_volatile(store, store.list_memories(), operation, exclude_ids=set()) == 1
    assert "valid_until" in store.records["old"]["metadata"]
    assert "valid_until" not in store.records["other"]["metadata"]
    assert "valid_until" not in store.records["fact"]["metadata"]


def test_review_mark_preserves_current_content_and_skips_deleted_memories() -> None:
    store = _Store([
        {"id": "updated", "content": "old promise"},
        {"id": "deleted", "content": "removed promise"},
    ])
    review = store.list_memories()
    store.update_memory({"id": "updated", "content": "completed promise"})
    store.delete_memory({"id": "deleted"})
    assert discipline.mark_reviewed(store, review) == 1
    assert store.records["updated"]["content"] == "completed promise"
    assert store.records["updated"]["metadata"]["expiry_reviewed"] is True
    assert "deleted" not in store.records
