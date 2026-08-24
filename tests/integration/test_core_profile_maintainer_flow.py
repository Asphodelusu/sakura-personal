"""P3.4 虚构对话下的 curator → 队列 → maintainer 集成场景。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.agent.core_profile_candidates import CoreCandidateQueue
from app.agent.core_profile_maintainer import (
    CoreMaintainerSettings,
    CoreMaintainerStateStore,
    CoreProfileMaintainer,
    MaintainerTrigger,
)
from app.agent.core_profile_maintainer_worker import (
    CoreMaintainerCompletionAdapter,
    maintainer_admission_indicates_work,
    persist_core_candidates,
)
from app.agent.memory import MemoryStore
from app.agent.memory_curator import MemoryCurator
from app.storage.chat_history import ChatHistoryEntry
from app.storage.paths import StoragePaths

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)
CREATED_AT = "2026-01-01T00:00:00+00:00"
UPDATED_AT = "2026-08-20T00:00:00+00:00"
SCOPE = "Sakura"


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeCurationClient:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def complete_raw(self, system_prompt, messages, **chat_params):  # type: ignore[no-untyped-def]
        self.calls.append(
            {"system_prompt": system_prompt, "messages": messages, "chat_params": chat_params}
        )
        return self.response


class FakeMaintainerClient:
    def __init__(self, response: str | BaseException) -> None:
        self.response = response
        self.calls: list[tuple[str, list[dict[str, str]]]] = []

    def complete_raw(self, system_prompt: str, messages: list[dict[str, str]], **kwargs: Any) -> str:
        self.calls.append((system_prompt, messages))
        if isinstance(self.response, BaseException):
            raise self.response
        return str(self.response)


class TrackingStore(MemoryStore):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.method_calls: list[str] = []
        self.patch_calls: list[tuple[Any, ...]] = []

    def core_profile(self) -> dict[str, Any] | None:
        self.method_calls.append("core_profile")
        return super().core_profile()

    def mood_state(self) -> dict[str, Any] | None:
        self.method_calls.append("mood_state")
        return {"content": "SECRET_MOOD"}

    def patch_core_profile_sections(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.method_calls.append("patch_core_profile_sections")
        self.patch_calls.append((args, kwargs))
        return super().patch_core_profile_sections(*args, **kwargs)


class FakeMemoryStore:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.deleted: list[dict[str, Any]] = []

    def list_memories(self, *, limit: int) -> list[dict[str, Any]]:
        return []

    def ensure_entity_index_backfilled(self, memories) -> None:  # type: ignore[no-untyped-def]
        return None

    def mood_history(self) -> list[dict[str, Any]]:
        return []

    def create_memory(self, arguments, *, allow_sensitive=False, wait=True):  # type: ignore[no-untyped-def]
        self.created.append(dict(arguments))
        return {"ok": True}

    def update_memory(self, arguments, *, allow_sensitive=False):  # type: ignore[no-untyped-def]
        self.updated.append(dict(arguments))
        return {}

    def delete_memory(self, arguments):  # type: ignore[no-untyped-def]
        self.deleted.append(dict(arguments))
        return {}


def _entry(role: str, content: str, created_at: str = "2026-05-31T12:00:00+08:00") -> ChatHistoryEntry:
    return ChatHistoryEntry(created_at=created_at, role=role, content=content)


def _candidate(**overrides: Any) -> dict[str, Any]:
    payload = {
        "op": "core_candidate",
        "kind": "explicit",
        "target_section": "今の関係",
        "subject_key": "relationship.identity",
        "claim": "我们明确确认了恋人关系。",
        "user_excerpt": "我们是恋人吧。",
        "assistant_excerpt": "嗯，是恋人。",
        "batch_id": "curation_flow_1",
        "confidence": 0.95,
        "observed_at": NOW.isoformat(),
    }
    payload.update(overrides)
    return payload


def _op(
    op: str,
    section: str,
    content: str,
    candidate_ids: list[str],
    evidence_ids: list[str],
) -> dict[str, Any]:
    return {
        "op": op,
        "section": section,
        "content": content,
        "reason": "更新认识",
        "candidate_ids": candidate_ids,
        "evidence_ids": evidence_ids,
    }


def _keep_op(candidate_id_value: str, evidence_id_value: str, section: str = "今の関係") -> dict[str, Any]:
    return {
        "op": "keep",
        "section": section,
        "reason": "不足以上改档案",
        "candidate_ids": [candidate_id_value],
        "evidence_ids": [evidence_id_value],
    }


def _proposal(operations: list[dict[str, Any]], base_updated_at: str = UPDATED_AT) -> str:
    return json.dumps({"base_updated_at": base_updated_at, "operations": operations}, ensure_ascii=False)


def _write_profile(tmp_path: Path, sections: dict[str, str]) -> None:
    path = StoragePaths(tmp_path).memory_core_profiles()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "id": f"core_profile:{SCOPE}",
        "schema_version": 2,
        "content": "旧缓存",
        "memory": "旧缓存",
        "sections": dict(sections),
        "metadata": {"created_at": CREATED_AT, "updated_at": UPDATED_AT, "source": "manual"},
    }
    path.write_text(json.dumps({SCOPE: record}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _saved(tmp_path: Path) -> dict[str, Any]:
    return json.loads(StoragePaths(tmp_path).memory_core_profiles().read_text(encoding="utf-8"))[SCOPE]


def _queue(tmp_path: Path, clock: Clock | None = None) -> CoreCandidateQueue:
    return CoreCandidateQueue(
        StoragePaths(tmp_path).memory_core_review_queue(),
        clock=clock or Clock(),
    )


def _maintainer(
    tmp_path: Path,
    client: FakeMaintainerClient,
    queue: CoreCandidateQueue,
    clock: Clock | None = None,
) -> tuple[CoreProfileMaintainer, TrackingStore]:
    tick = clock or Clock()
    store = TrackingStore(base_dir=tmp_path, scope_id=SCOPE, memory_client=object())
    maintainer = CoreProfileMaintainer(
        api_client=CoreMaintainerCompletionAdapter(client),
        memory_store=store,
        queue=queue,
        state_store=CoreMaintainerStateStore(
            StoragePaths(tmp_path).memory_core_maintainer_state(),
            clock=tick,
        ),
        settings=CoreMaintainerSettings(),
        clock=tick,
    )
    return maintainer, store


def _curate(operations: list[dict[str, Any]], entries: list[ChatHistoryEntry]) -> tuple[Any, FakeMemoryStore, FakeCurationClient]:
    store = FakeMemoryStore()
    client = FakeCurationClient(json.dumps({"operations": operations}, ensure_ascii=False))
    curator = MemoryCurator(client, store)
    return curator.curate_entries(entries), store, client


def test_bilateral_relationship_confirmation_applies(tmp_path: Path) -> None:
    _write_profile(tmp_path, {"今の関係": ""})
    result, memory_store, curator_client = _curate(
        [_candidate()],
        [
            _entry("user", "我们是恋人吧。"),
            _entry("assistant", "嗯，是恋人。", "2026-05-31T12:01:00+08:00"),
        ],
    )
    assert memory_store.created == []
    assert memory_store.updated == []
    assert len(result.core_candidates) == 1

    queue = _queue(tmp_path)
    trigger = persist_core_candidates(queue, SCOPE, result.core_candidates)
    assert trigger is not None and trigger.kind == "explicit"
    assert maintainer_admission_indicates_work(
        queue=queue,
        state_store=CoreMaintainerStateStore(StoragePaths(tmp_path).memory_core_maintainer_state()),
        settings=CoreMaintainerSettings(),
        scope_id=SCOPE,
        trigger=trigger,
        busy=False,
        now=NOW,
    )

    stored = queue.candidates_for(SCOPE)[0]
    client = FakeMaintainerClient(
        _proposal(
            [
                _op(
                    "replace",
                    "今の関係",
                    "我们明确确认了恋人关系。",
                    [stored.id],
                    [stored.evidence[0].id],
                )
            ]
        )
    )
    maintainer, tracking = _maintainer(tmp_path, client, queue)
    applied = maintainer.run_once(SCOPE, trigger)
    assert applied.status == "applied"
    assert _saved(tmp_path)["sections"]["今の関係"] == "我们明确确认了恋人关系。"
    assert tracking.patch_calls
    assert curator_client.calls
    assert "SECRET_MOOD" not in client.calls[0][0]
    assert "history" not in client.calls[0][0].lower()
    assert "mood_state" not in tracking.method_calls


def test_transient_jealousy_does_not_create_explicit_core(tmp_path: Path) -> None:
    _write_profile(tmp_path, {"今の関係": "我们是恋人。"})
    result, memory_store, _ = _curate(
        [
            _candidate(
                kind="observed",
                target_section="今の私",
                subject_key="mood.jealousy",
                claim="我今晚吃醋了，但明天就会好。",
                user_excerpt="你是不是更在意别人？",
                assistant_excerpt="我今晚有点吃醋。",
                confidence=0.86,
            )
        ],
        [
            _entry("user", "你是不是更在意别人？"),
            _entry("assistant", "我今晚有点吃醋。", "2026-05-31T12:01:00+08:00"),
        ],
    )
    assert result.core_candidates == ()
    assert memory_store.created == []
    queue = _queue(tmp_path)
    trigger = persist_core_candidates(queue, SCOPE, result.core_candidates)
    assert trigger is None
    assert queue.candidates_for(SCOPE) == ()
    assert _saved(tmp_path)["sections"]["今の関係"] == "我们是恋人。"
    assert not maintainer_admission_indicates_work(
        queue=queue,
        state_store=CoreMaintainerStateStore(StoragePaths(tmp_path).memory_core_maintainer_state()),
        settings=CoreMaintainerSettings(),
        scope_id=SCOPE,
        trigger=None,
        busy=False,
        now=NOW,
    )


def test_repeated_observed_behavior_waits_for_thresholds(tmp_path: Path) -> None:
    _write_profile(tmp_path, {"今の私": "我还不太会把不安说出来。"})
    first = _candidate(
        kind="observed",
        target_section="今の私",
        subject_key="relationship.trust",
        claim="我开始更愿意把不安说出来。",
        user_excerpt="你可以慢慢说。",
        assistant_excerpt="我想试着把不安说出来。",
        confidence=0.85,
        batch_id="curation_obs_0",
        observed_at=NOW.isoformat(),
    )
    result, _, _ = _curate(
        [first],
        [
            _entry("user", "你可以慢慢说。"),
            _entry("assistant", "我想试着把不安说出来。", "2026-05-31T12:01:00+08:00"),
        ],
    )
    queue = _queue(tmp_path)
    trigger = persist_core_candidates(queue, SCOPE, result.core_candidates)
    assert trigger is None
    assert queue.candidates_for(SCOPE)[0].status == "pending"
    assert not maintainer_admission_indicates_work(
        queue=queue,
        state_store=CoreMaintainerStateStore(StoragePaths(tmp_path).memory_core_maintainer_state()),
        settings=CoreMaintainerSettings(),
        scope_id=SCOPE,
        trigger=None,
        busy=False,
        now=NOW,
    )
    assert _saved(tmp_path)["sections"]["今の私"] == "我还不太会把不安说出来。"

    clock = Clock(NOW + timedelta(minutes=40))
    later_queue = CoreCandidateQueue(
        StoragePaths(tmp_path).memory_core_review_queue(),
        clock=clock,
    )
    for index in (1, 2):
        later_queue.ingest(
            SCOPE,
            {
                "kind": "observed",
                "target_section": "今の私",
                "subject_key": "relationship.trust",
                "claim": "我开始更愿意把不安说出来。",
                "user_excerpt": f"用户习惯{index}",
                "assistant_excerpt": f"我的观察{index}",
                "batch_id": f"curation_obs_{index}",
                "confidence": 0.85,
                "observed_at": (NOW + timedelta(minutes=20 * index)).isoformat(),
            },
        )
    stored = later_queue.eligible_for(SCOPE)
    assert stored
    client = FakeMaintainerClient(
        _proposal(
            [
                _op(
                    "refine",
                    "今の私",
                    "我开始更愿意把不安说出来。",
                    [stored[0].id],
                    [stored[0].evidence[-1].id],
                )
            ]
        )
    )
    maintainer, _ = _maintainer(tmp_path, client, later_queue, clock=clock)
    applied = maintainer.run_once(
        SCOPE,
        MaintainerTrigger(kind="observed", candidate_id=stored[0].id),
    )
    assert applied.status == "applied"
    assert _saved(tmp_path)["sections"]["今の私"] == "我开始更愿意把不安说出来。"


def test_corrected_address_replaces_rather_than_appends(tmp_path: Path) -> None:
    _write_profile(tmp_path, {"大切な約束と境界": "他叫我「小樱」。"})
    result, _, _ = _curate(
        [
            _candidate(
                kind="explicit",
                target_section="大切な約束と境界",
                subject_key="relationship.correction",
                claim="他纠正称呼，叫我樱，而不是小樱。",
                user_excerpt="以后叫我「樱」。",
                assistant_excerpt="好，我叫你「樱」。",
                batch_id="curation_address",
            )
        ],
        [
            _entry("user", "以后叫我「樱」。"),
            _entry("assistant", "好，我叫你「樱」。", "2026-05-31T12:01:00+08:00"),
        ],
    )
    queue = _queue(tmp_path)
    trigger = persist_core_candidates(queue, SCOPE, result.core_candidates)
    stored = queue.candidates_for(SCOPE)[0]
    client = FakeMaintainerClient(
        _proposal(
            [
                _op(
                    "replace",
                    "大切な約束と境界",
                    "他叫我「樱」。",
                    [stored.id],
                    [stored.evidence[0].id],
                )
            ]
        )
    )
    maintainer, _ = _maintainer(tmp_path, client, queue)
    applied = maintainer.run_once(SCOPE, trigger)
    assert applied.status == "applied"
    body = _saved(tmp_path)["sections"]["大切な約束と境界"]
    assert body == "他叫我「樱」。"
    assert "小樱" not in body
    assert body.count("叫我") == 1


def test_repeated_affection_yields_keep(tmp_path: Path) -> None:
    _write_profile(tmp_path, {"今の関係": "我们是恋人。"})
    result, _, _ = _curate(
        [
            _candidate(
                kind="explicit",
                subject_key="relationship.identity",
                claim="我们明确确认了恋人关系。",
                user_excerpt="我还是喜欢你。",
                assistant_excerpt="我也喜欢你。",
                batch_id="curation_affection",
            )
        ],
        [
            _entry("user", "我还是喜欢你。"),
            _entry("assistant", "我也喜欢你。", "2026-05-31T12:01:00+08:00"),
        ],
    )
    queue = _queue(tmp_path)
    trigger = persist_core_candidates(queue, SCOPE, result.core_candidates)
    stored = queue.candidates_for(SCOPE)[0]
    client = FakeMaintainerClient(_proposal([_keep_op(stored.id, stored.evidence[0].id)]))
    maintainer, tracking = _maintainer(tmp_path, client, queue)
    kept = maintainer.run_once(SCOPE, trigger)
    assert kept.status == "keep"
    assert tracking.patch_calls == []
    assert _saved(tmp_path)["sections"]["今の関係"] == "我们是恋人。"
    assert queue.candidates_for(SCOPE)[0].status == "reviewed"


def test_maintainer_failure_does_not_fail_ordinary_curation(tmp_path: Path) -> None:
    result, memory_store, _ = _curate(
        [
            {
                "op": "add",
                "layer": "semantic",
                "content": "他默认用中文和我说话。",
                "evidence": "以后默认中文和我说话",
                "confidence": 0.9,
            },
            _candidate(),
        ],
        [
            _entry("user", "以后默认中文和我说话"),
            _entry("user", "我们是恋人吧。"),
            _entry("assistant", "嗯，是恋人。", "2026-05-31T12:01:00+08:00"),
        ],
    )
    assert result.created == 1
    assert memory_store.created[0]["layer"] == "semantic"
    queue = _queue(tmp_path)
    trigger = persist_core_candidates(queue, SCOPE, result.core_candidates)
    _write_profile(tmp_path, {"今の関係": ""})
    client = FakeMaintainerClient(RuntimeError("maintainer api down"))
    maintainer, tracking = _maintainer(tmp_path, client, queue)
    failed = maintainer.run_once(SCOPE, trigger)
    assert failed.status == "api_error"
    assert result.created == 1
    assert queue.candidates_for(SCOPE)[0].status == "pending"
    assert tracking.patch_calls == []


def test_busy_admission_defers_with_candidates_preserved(tmp_path: Path) -> None:
    result, _, _ = _curate(
        [_candidate()],
        [
            _entry("user", "我们是恋人吧。"),
            _entry("assistant", "嗯，是恋人。", "2026-05-31T12:01:00+08:00"),
        ],
    )
    queue = _queue(tmp_path)
    trigger = persist_core_candidates(queue, SCOPE, result.core_candidates)
    assert trigger is not None
    assert not maintainer_admission_indicates_work(
        queue=queue,
        state_store=CoreMaintainerStateStore(StoragePaths(tmp_path).memory_core_maintainer_state()),
        settings=CoreMaintainerSettings(),
        scope_id=SCOPE,
        trigger=trigger,
        busy=True,
        now=NOW,
    )
    assert queue.candidates_for(SCOPE)[0].status == "pending"
