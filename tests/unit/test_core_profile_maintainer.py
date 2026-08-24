"""tests/unit/test_core_profile_maintainer.py — P3.3 同步 maintainer core 单测。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.agent.core_profile_candidates import (
    CoreCandidateQueue,
    CoreCandidateQueueError,
)
from app.agent.core_profile_maintainer import (
    CoreMaintainerScheduler,
    CoreMaintainerSettings,
    CoreMaintainerStateError,
    CoreMaintainerStateStore,
    CoreProfileMaintainer,
    MaintainerParseError,
    MaintainerTrigger,
    parse_maintainer_response,
)
from app.agent.memory import CoreProfileStorageError, MemoryStore
from app.storage.paths import StoragePaths

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)
CREATED_AT = "2026-01-01T00:00:00+00:00"
UPDATED_AT = "2026-08-20T00:00:00+00:00"
SCOPE = "Sakura"
SECRET_CLAIM = "SECRET_CLAIM_FICTIONAL"
SECRET_EXCERPT = "SECRET_EXCERPT_FICTIONAL"
SECRET_PROFILE = "SECRET_PROFILE_FICTIONAL"
SECRET_REASON = "SECRET_REASON_FICTIONAL"
SECRET_MOOD = "SECRET_MOOD_FICTIONAL"


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeClient:
    def __init__(self, response: str | BaseException | None = None) -> None:
        self.calls: list[tuple[str, list[dict[str, str]], dict[str, Any]]] = []
        self.response = response if response is not None else _proposal(UPDATED_AT, [_keep_op("cc_x", "ce_y")])

    def complete_raw(self, system_prompt: str, messages: list[dict[str, str]], **kwargs: Any) -> str:
        self.calls.append((system_prompt, messages, kwargs))
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
        return {"content": SECRET_MOOD}

    def patch_core_profile_sections(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.method_calls.append("patch_core_profile_sections")
        self.patch_calls.append((args, kwargs))
        return super().patch_core_profile_sections(*args, **kwargs)


def _queue_path(tmp_path: Path) -> Path:
    return StoragePaths(tmp_path).memory_core_review_queue()


def _state_path(tmp_path: Path) -> Path:
    return StoragePaths(tmp_path).memory_core_maintainer_state()


def _core_path(tmp_path: Path) -> Path:
    return StoragePaths(tmp_path).memory_core_profiles()


def _explicit(**overrides: Any) -> dict[str, Any]:
    payload = {
        "kind": "explicit",
        "target_section": "今の関係",
        "subject_key": "relationship.identity",
        "claim": "我们明确确认了恋人关系。",
        "user_excerpt": "我们是恋人吧。",
        "assistant_excerpt": "嗯，是恋人。",
        "batch_id": "curation_batch_1",
        "confidence": 0.95,
        "observed_at": NOW.isoformat(),
    }
    payload.update(overrides)
    return payload


def _observed(**overrides: Any) -> dict[str, Any]:
    payload = _explicit(
        kind="observed",
        target_section="今の私",
        subject_key="relationship.trust",
        claim="我开始更愿意把不安说出来。",
        confidence=0.85,
    )
    payload.update(overrides)
    return payload


def _ingest_observed(
    queue: CoreCandidateQueue,
    *,
    count: int = 3,
    batches: int = 2,
    span_minutes: float = 30,
    confidence: float = 0.85,
    subject_key: str = "relationship.trust",
    target_section: str = "今の私",
    start: datetime | None = None,
    claim: str = "我开始更愿意把不安说出来。",
) -> Any:
    origin = start or (NOW - timedelta(minutes=span_minutes))
    last = None
    for index in range(count):
        observed_at = origin if count == 1 else origin + timedelta(minutes=span_minutes) * index / (count - 1)
        last = queue.ingest(
            SCOPE,
            _observed(
                subject_key=subject_key,
                target_section=target_section,
                claim=claim,
                user_excerpt=f"用户证据{index}",
                assistant_excerpt=f"我的回应{index}",
                batch_id=f"curation_{subject_key}_{index % batches}",
                confidence=confidence,
                observed_at=observed_at.isoformat(),
            ),
        )
    return last


def _op(
    op: str,
    section: str,
    content: str,
    candidate_ids: list[str],
    evidence_ids: list[str],
    reason: str = "更新认识",
) -> dict[str, Any]:
    return {
        "op": op,
        "section": section,
        "content": content,
        "reason": reason,
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


def _migrate_op(
    sections: dict[str, str],
    candidate_ids: list[str],
    evidence_ids: list[str],
    reason: str = "迁移旧档案",
) -> dict[str, Any]:
    return {
        "op": "migrate_legacy",
        "sections": sections,
        "reason": reason,
        "candidate_ids": candidate_ids,
        "evidence_ids": evidence_ids,
    }


def _proposal(base_updated_at: str, operations: list[dict[str, Any]]) -> str:
    return json.dumps({"base_updated_at": base_updated_at, "operations": operations}, ensure_ascii=False)


def _formal_v2(sections: dict[str, str], *, content: str = "旧缓存") -> dict[str, Any]:
    return {
        "schema_version": 2,
        "content": content,
        "memory": content,
        "sections": dict(sections),
        "metadata": {"created_at": CREATED_AT, "updated_at": UPDATED_AT, "source": "manual"},
    }


def _write_profile(tmp_path: Path, record: dict[str, Any]) -> Path:
    path = _core_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"id": f"core_profile:{SCOPE}", **record}
    path.write_text(json.dumps({SCOPE: payload}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _saved_record(tmp_path: Path) -> dict[str, Any]:
    return json.loads(_core_path(tmp_path).read_text(encoding="utf-8"))[SCOPE]


def _write_formal(tmp_path: Path, sections: dict[str, str] | None = None) -> None:
    _write_profile(
        tmp_path,
        _formal_v2(
            sections
            or {
                "今の関係": "我们是恋人。他叫我「小樱」。",
                "今の私": f"我愿意把不安说出来。{SECRET_PROFILE}",
            }
        ),
    )


def _harness(
    tmp_path: Path,
    *,
    now: datetime | Clock = NOW,
    response: str | BaseException | None = None,
    settings: CoreMaintainerSettings | None = None,
    store: MemoryStore | None = None,
    queue_writer=None,
) -> tuple[CoreProfileMaintainer, FakeClient, CoreCandidateQueue, Clock, MemoryStore]:
    clock = now if isinstance(now, Clock) else Clock(now)
    client = FakeClient(response)
    queue = CoreCandidateQueue(_queue_path(tmp_path), clock=clock, writer=queue_writer)
    state = CoreMaintainerStateStore(_state_path(tmp_path), clock=clock)
    memory_store = store or TrackingStore(base_dir=tmp_path, scope_id=SCOPE, memory_client=object())
    maintainer = CoreProfileMaintainer(
        api_client=client,
        memory_store=memory_store,
        queue=queue,
        state_store=state,
        settings=settings or CoreMaintainerSettings(),
        clock=clock,
    )
    return maintainer, client, queue, clock, memory_store


class TestConfigNormalization:
    def test_defaults_and_candidate_config_conversion(self) -> None:
        settings = CoreMaintainerSettings().normalized()
        config = settings.to_candidate_config()
        assert settings.enabled is True
        assert settings.observed_min_evidence == 3
        assert settings.max_candidates_per_call == 5
        assert settings.lease_ttl_minutes == 30
        assert config.observed_min_evidence == 3
        assert config.observed_min_batches == 2
        assert config.observed_min_span_minutes == 30
        assert config.observed_min_confidence == pytest.approx(0.80)

    def test_invalid_values_are_clamped(self) -> None:
        settings = CoreMaintainerSettings(
            observed_min_confidence=1.4,
            max_candidates_per_call=9,
            max_sections_per_call=0,
            pause_after_validation_failures=-3,
            normal_cooldown_hours=-1,
            lease_ttl_minutes=0,
        ).normalized()
        assert settings.observed_min_confidence == 1.0
        assert settings.max_candidates_per_call == 5
        assert settings.max_sections_per_call == 1
        assert settings.pause_after_validation_failures == 1
        assert settings.normal_cooldown_hours == 0
        assert settings.lease_ttl_minutes == 1


class TestDisabledAndNoEligible:
    def test_disabled_makes_zero_calls(self, tmp_path: Path) -> None:
        _write_formal(tmp_path)
        maintainer, client, queue, _, store = _harness(
            tmp_path, settings=CoreMaintainerSettings(enabled=False)
        )
        queue.ingest(SCOPE, _explicit())
        result = maintainer.run_once(SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1"))
        assert result.status == "skipped_disabled"
        assert client.calls == []
        assert isinstance(store, TrackingStore)
        assert "core_profile" not in store.method_calls

    def test_no_eligible_makes_zero_calls(self, tmp_path: Path) -> None:
        _write_formal(tmp_path)
        maintainer, client, queue, _, store = _harness(tmp_path)
        queue.ingest(SCOPE, _explicit(user_excerpt=""))
        result = maintainer.run_once(SCOPE, MaintainerTrigger(kind="observed"))
        assert result.status == "skipped_no_eligible"
        assert client.calls == []
        assert isinstance(store, TrackingStore)
        assert "core_profile" not in store.method_calls


class TestSchedulerAdmission:
    def test_observed_trigger_then_cooldown_preserves_pending(self, tmp_path: Path) -> None:
        _write_formal(tmp_path)
        clock = Clock(NOW)
        maintainer, client, queue, _, _ = _harness(tmp_path, now=clock, response=TimeoutError("timed out"))
        candidate = _ingest_observed(queue)
        first = maintainer.run_once(SCOPE, MaintainerTrigger(kind="observed", candidate_id=candidate.id))
        assert first.status == "api_error"
        assert len(client.calls) == 1
        assert queue.candidates_for(SCOPE)[0].status == "pending"

        client.response = _proposal(UPDATED_AT, [_keep_op(candidate.id, candidate.evidence[0].id, "今の私")])
        clock.now = NOW + timedelta(hours=1)
        cooled = maintainer.run_once(SCOPE, MaintainerTrigger(kind="observed", candidate_id=candidate.id))
        assert cooled.status == "skipped_cooldown"
        assert len(client.calls) == 1

        clock.now = NOW + timedelta(hours=6)
        resumed = maintainer.run_once(SCOPE)
        assert resumed.status == "keep"
        assert len(client.calls) == 2
        assert queue.candidates_for(SCOPE)[0].status == "reviewed"

    def test_three_eligible_admits_a_run(self, tmp_path: Path) -> None:
        _write_formal(tmp_path)
        maintainer, client, queue, _, _ = _harness(tmp_path)
        items = [
            _ingest_observed(queue, subject_key="relationship.trust"),
            _ingest_observed(queue, subject_key="relationship.reliance", target_section="今の私"),
            _ingest_observed(queue, subject_key="relationship.habit", target_section="今の私"),
        ]
        client.response = _proposal(
            UPDATED_AT,
            [_keep_op(item.id, item.evidence[0].id, item.target_section) for item in items],
        )
        result = maintainer.run_once(SCOPE)
        assert result.status == "keep"
        assert len(client.calls) == 1

    def test_stale_uses_eligible_since_not_first_seen(self, tmp_path: Path) -> None:
        _write_formal(tmp_path)
        clock = Clock(NOW)
        maintainer, client, queue, _, _ = _harness(tmp_path, now=clock)
        start = NOW - timedelta(hours=80)
        candidate = _ingest_observed(queue, span_minutes=40, start=start)
        since = datetime.fromisoformat(candidate.evidence[2].observed_at)
        assert since == start + timedelta(minutes=40)
        clock.now = since + timedelta(hours=71)
        early = maintainer.run_once(SCOPE)
        assert early.status == "skipped_no_eligible"
        assert client.calls == []
        assert isinstance(maintainer.memory_store, TrackingStore)
        assert "core_profile" not in maintainer.memory_store.method_calls
        clock.now = since + timedelta(hours=72)
        client.response = _proposal(UPDATED_AT, [_keep_op(candidate.id, candidate.evidence[0].id, "今の私")])
        stale = maintainer.run_once(SCOPE)
        assert stale.status == "keep"
        assert len(client.calls) == 1

    def test_explicit_bypass_once_even_after_api_failure(self, tmp_path: Path) -> None:
        _write_formal(tmp_path)
        clock = Clock(NOW)
        maintainer, client, queue, _, _ = _harness(tmp_path, now=clock, response=TimeoutError("timed out"))
        first = queue.ingest(SCOPE, _explicit(batch_id="curation_a"))
        maintainer.run_once(SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_a", candidate_id=first.id))
        clock.now = NOW + timedelta(hours=1)
        same_batch = maintainer.run_once(
            SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_a", candidate_id=first.id)
        )
        assert same_batch.status == "skipped_cooldown"
        second = queue.ingest(
            SCOPE,
            _explicit(
                subject_key="relationship.address",
                claim="我叫他你。",
                batch_id="curation_b",
                observed_at=(NOW + timedelta(hours=1)).isoformat(),
            ),
        )
        bypass = maintainer.run_once(
            SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_b", candidate_id=second.id)
        )
        assert bypass.status == "api_error"
        assert len(client.calls) == 2
        again = maintainer.run_once(
            SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_b", candidate_id=second.id)
        )
        assert again.status == "skipped_cooldown"
        assert len(client.calls) == 2

    def test_pause_outranks_explicit_bypass(self, tmp_path: Path) -> None:
        _write_formal(tmp_path)
        clock = Clock(NOW)
        settings = CoreMaintainerSettings(pause_after_validation_failures=1, pause_hours=24)
        maintainer, client, queue, _, _ = _harness(tmp_path, now=clock, settings=settings)
        candidate = queue.ingest(SCOPE, _explicit())
        client.response = "{not-json"
        rejected = maintainer.run_once(
            SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1", candidate_id=candidate.id)
        )
        assert rejected.status == "rejected"
        clock.now = NOW + timedelta(hours=1)
        later = queue.ingest(
            SCOPE,
            _explicit(
                subject_key="relationship.boundary",
                claim="晚上十一点后不打电话。",
                batch_id="curation_new",
                observed_at=clock.now.isoformat(),
            ),
        )
        paused = maintainer.run_once(
            SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_new", candidate_id=later.id)
        )
        assert paused.status == "skipped_paused"
        assert len(client.calls) == 1
        assert isinstance(maintainer.memory_store, TrackingStore)
        assert maintainer.memory_store.method_calls.count("core_profile") == 1

    def test_global_busy_preserves_trigger(self, tmp_path: Path) -> None:
        _write_formal(tmp_path)
        clock = Clock(NOW)
        state = CoreMaintainerStateStore(_state_path(tmp_path), clock=clock)
        assert state.try_acquire_lease("other-holder") is True
        maintainer, client, queue, _, _ = _harness(tmp_path, now=clock)
        candidate = queue.ingest(SCOPE, _explicit())
        busy = maintainer.run_once(
            SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1", candidate_id=candidate.id)
        )
        assert busy.status == "skipped_busy"
        assert client.calls == []
        assert isinstance(maintainer.memory_store, TrackingStore)
        assert "core_profile" not in maintainer.memory_store.method_calls
        state.release_lease("other-holder")
        client.response = _proposal(UPDATED_AT, [_keep_op(candidate.id, candidate.evidence[0].id)])
        resumed = maintainer.run_once(SCOPE)
        assert resumed.status == "keep"
        assert len(client.calls) == 1


class TestSelectionOrder:
    def test_caps_at_five_in_stable_priority_order(self, tmp_path: Path) -> None:
        _write_formal(tmp_path)
        clock = Clock(NOW)
        queue = CoreCandidateQueue(_queue_path(tmp_path), clock=clock)
        state = CoreMaintainerStateStore(_state_path(tmp_path), clock=clock)
        scheduler = CoreMaintainerScheduler(queue, state, CoreMaintainerSettings(), clock)
        triggering = queue.ingest(
            SCOPE,
            _explicit(observed_at=(NOW - timedelta(hours=1)).isoformat(), batch_id="curation_trigger"),
        )
        other_explicit = queue.ingest(
            SCOPE,
            _explicit(
                subject_key="relationship.address",
                claim="我叫他你。",
                observed_at=(NOW - timedelta(hours=10)).isoformat(),
                batch_id="curation_old_explicit",
            ),
        )
        stale = _ingest_observed(
            queue,
            subject_key="relationship.stale",
            start=NOW - timedelta(hours=80),
            span_minutes=40,
        )
        pending = _ingest_observed(
            queue,
            subject_key="relationship.pending_trigger",
            start=NOW - timedelta(hours=8),
            span_minutes=40,
        )
        other_a = _ingest_observed(
            queue,
            subject_key="relationship.other_a",
            start=NOW - timedelta(hours=6),
            span_minutes=40,
        )
        other_b = _ingest_observed(
            queue,
            subject_key="relationship.other_b",
            start=NOW - timedelta(hours=5),
            span_minutes=40,
        )
        state.add_pending_trigger(
            SCOPE,
            MaintainerTrigger(kind="observed", candidate_id=pending.id),
        )
        state.add_pending_trigger(
            SCOPE,
            MaintainerTrigger(kind="observed", candidate_id=other_b.id),
        )
        decision = scheduler.evaluate(
            SCOPE,
            MaintainerTrigger(kind="explicit", batch_id="curation_trigger", candidate_id=triggering.id),
        )
        try:
            assert decision.admitted is True
            assert [item.id for item in decision.selected] == [
                triggering.id,
                other_explicit.id,
                stale.id,
                pending.id,
                other_b.id,
            ]
            assert other_a.id not in {item.id for item in decision.selected}
            leftover = {item.candidate_id for item in state.pending_triggers(SCOPE)}
            assert leftover == set()
            assert state.explicit_batch_used(SCOPE, "curation_trigger") is True
            assert state.explicit_batch_used(SCOPE, "curation_old_explicit") is True
        finally:
            scheduler.release(decision.lease_holder)

    def test_unselected_explicit_batch_is_not_consumed(self, tmp_path: Path) -> None:
        clock = Clock(NOW)
        queue = CoreCandidateQueue(_queue_path(tmp_path), clock=clock)
        state = CoreMaintainerStateStore(_state_path(tmp_path), clock=clock)
        scheduler = CoreMaintainerScheduler(queue, state, CoreMaintainerSettings(), clock)
        items = []
        for index in range(6):
            item = queue.ingest(
                SCOPE,
                _explicit(
                    subject_key=f"relationship.identity.{index}",
                    claim=f"确认关系{index}。",
                    batch_id=f"batch_{index}",
                    observed_at=(NOW - timedelta(minutes=index)).isoformat(),
                ),
            )
            items.append(item)
            state.add_pending_trigger(
                SCOPE,
                MaintainerTrigger(kind="explicit", batch_id=f"batch_{index}", candidate_id=item.id),
            )
        decision = scheduler.evaluate(
            SCOPE,
            MaintainerTrigger(kind="explicit", batch_id="batch_0", candidate_id=items[0].id),
        )
        try:
            selected_ids = {item.id for item in decision.selected}
            assert len(selected_ids) == 5
            unselected = [item for item in items if item.id not in selected_ids]
            assert len(unselected) == 1
            leftover = {item.candidate_id for item in state.pending_triggers(SCOPE)}
            assert unselected[0].id in leftover
            assert state.explicit_batch_used(SCOPE, unselected[0].evidence[0].batch_id) is False
            for item in decision.selected:
                assert item.id not in leftover
                assert state.explicit_batch_used(SCOPE, item.evidence[0].batch_id) is True
        finally:
            scheduler.release(decision.lease_holder)


class TestLeaseExpiry:
    def test_expired_global_lease_can_be_stolen_after_ttl(self, tmp_path: Path) -> None:
        _write_formal(tmp_path)
        clock = Clock(NOW)
        state = CoreMaintainerStateStore(_state_path(tmp_path), clock=clock)
        assert state.try_acquire_lease("crashed-holder") is True
        maintainer, client, queue, _, store = _harness(tmp_path, now=clock)
        candidate = queue.ingest(SCOPE, _explicit())
        clock.now = NOW + timedelta(minutes=29)
        still_busy = maintainer.run_once(
            SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1", candidate_id=candidate.id)
        )
        assert still_busy.status == "skipped_busy"
        assert client.calls == []
        assert isinstance(store, TrackingStore)
        assert "core_profile" not in store.method_calls
        clock.now = NOW + timedelta(minutes=30)
        client.response = _proposal(UPDATED_AT, [_keep_op(candidate.id, candidate.evidence[0].id)])
        recovered = maintainer.run_once(
            SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1", candidate_id=candidate.id)
        )
        assert recovered.status == "keep"
        assert len(client.calls) == 1


class TestPromptPrivacy:
    def test_prompt_uses_profile_sections_and_candidates_only(self, tmp_path: Path) -> None:
        _write_formal(tmp_path)
        maintainer, client, queue, _, store = _harness(tmp_path)
        candidate = queue.ingest(
            SCOPE,
            _explicit(claim=SECRET_CLAIM, user_excerpt=SECRET_EXCERPT, assistant_excerpt="嗯，是恋人。"),
        )
        client.response = _proposal(UPDATED_AT, [_keep_op(candidate.id, candidate.evidence[0].id)])
        maintainer.run_once(SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1"))
        assert isinstance(store, TrackingStore)
        assert store.method_calls[0] == "core_profile"
        assert "mood_state" not in store.method_calls
        system_prompt, messages, _kwargs = client.calls[0]
        snapshot = system_prompt + messages[0]["content"]
        assert "今の関係" in snapshot
        assert SECRET_PROFILE in snapshot
        assert SECRET_CLAIM in snapshot
        assert SECRET_EXCERPT in snapshot
        assert SECRET_MOOD not in snapshot
        assert "intimacy" not in snapshot.lower()
        assert "人格卡" not in snapshot
        assert "聊天历史" not in snapshot
        assert "你是 Sakura" in snapshot or "あなたは Sakura" in snapshot


class TestStrictParser:
    def test_rejects_extra_keys_wrong_types_and_markdown(self) -> None:
        with pytest.raises(MaintainerParseError):
            parse_maintainer_response("```json\n" + _proposal(UPDATED_AT, [_keep_op("cc_x", "ce_y")]) + "\n```")
        with pytest.raises(MaintainerParseError):
            parse_maintainer_response(json.dumps({"base_updated_at": UPDATED_AT, "operations": [], "note": "x"}))
        with pytest.raises(MaintainerParseError):
            parse_maintainer_response(json.dumps({"base_updated_at": UPDATED_AT, "operations": {"op": "keep"}}))
        with pytest.raises(MaintainerParseError):
            parse_maintainer_response(
                json.dumps(
                    {
                        "base_updated_at": UPDATED_AT,
                        "operations": [
                            {
                                "op": "keep",
                                "section": "今の関係",
                                "content": "",
                                "reason": "x",
                                "candidate_ids": "cc_x",
                                "evidence_ids": ["ce_y"],
                            }
                        ],
                    }
                )
            )
        with pytest.raises(MaintainerParseError):
            parse_maintainer_response(
                json.dumps(
                    {
                        "base_updated_at": UPDATED_AT,
                        "operations": [
                            _op("archive", "今の関係", "x", ["cc_x"], ["ce_y"])
                        ],
                    }
                )
            )

    def test_keep_forbids_content_and_refs_must_be_nonempty_unique(self) -> None:
        with pytest.raises(MaintainerParseError):
            parse_maintainer_response(
                _proposal(
                    UPDATED_AT,
                    [
                        {
                            "op": "keep",
                            "section": "今の関係",
                            "content": "",
                            "reason": "x",
                            "candidate_ids": ["cc_x"],
                            "evidence_ids": ["ce_y"],
                        }
                    ],
                )
            )
        with pytest.raises(MaintainerParseError):
            parse_maintainer_response(
                _proposal(
                    UPDATED_AT,
                    [
                        {
                            "op": "keep",
                            "section": "今の関係",
                            "reason": "x",
                            "candidate_ids": [],
                            "evidence_ids": ["ce_y"],
                        }
                    ],
                )
            )
        with pytest.raises(MaintainerParseError):
            parse_maintainer_response(
                _proposal(
                    UPDATED_AT,
                    [
                        {
                            "op": "refine",
                            "section": "今の関係",
                            "content": "我们是恋人。",
                            "reason": "x",
                            "candidate_ids": ["cc_x", "cc_x"],
                            "evidence_ids": ["ce_y"],
                        }
                    ],
                )
            )
        with pytest.raises(MaintainerParseError):
            parse_maintainer_response(
                _proposal(
                    UPDATED_AT,
                    [
                        {
                            "op": "refine",
                            "section": "今の関係",
                            "content": "我们是恋人。",
                            "reason": "x",
                            "candidate_ids": ["cc_x"],
                            "evidence_ids": [True],
                        }
                    ],
                )
            )

    def test_refine_replace_remove_content_rules_are_parse_errors(self) -> None:
        with pytest.raises(MaintainerParseError):
            parse_maintainer_response(
                _proposal(UPDATED_AT, [_op("refine", "今の関係", "", ["cc_x"], ["ce_y"])])
            )
        with pytest.raises(MaintainerParseError):
            parse_maintainer_response(
                _proposal(UPDATED_AT, [_op("replace", "今の関係", "", ["cc_x"], ["ce_y"])])
            )
        with pytest.raises(MaintainerParseError):
            parse_maintainer_response(
                _proposal(UPDATED_AT, [_op("remove", "今の関係", "残留", ["cc_x"], ["ce_y"])])
            )
        with pytest.raises(MaintainerParseError):
            parse_maintainer_response(
                json.dumps(
                    {
                        "base_updated_at": UPDATED_AT,
                        "operations": [
                            {
                                "op": "refine",
                                "section": "今の関係",
                                "content": 12,
                                "reason": "x",
                                "candidate_ids": ["cc_x"],
                                "evidence_ids": ["ce_y"],
                            }
                        ],
                    }
                )
            )

    def test_legacy_per_section_migrate_schema_is_parse_error(self) -> None:
        with pytest.raises(MaintainerParseError):
            parse_maintainer_response(
                _proposal(
                    UPDATED_AT,
                    [_op("migrate_legacy", "今の関係", "我们是恋人。", ["cc_e"], ["ce_e"])],
                )
            )

    def test_accepts_strict_keep_refine_replace_remove_migrate(self) -> None:
        parsed = parse_maintainer_response(
            _proposal(
                UPDATED_AT,
                [
                    _keep_op("cc_a", "ce_a"),
                    _op("refine", "今の関係", "我们是恋人。", ["cc_b"], ["ce_b"]),
                    _op("replace", "今の私", "我更愿意说出来。", ["cc_c"], ["ce_c"]),
                    _op("remove", "大切な約束と境界", "", ["cc_d"], ["ce_d"]),
                    _migrate_op(
                        {"今の関係": "我们是恋人。", "今の私": "我愿意把不安说出来。"},
                        ["cc_e"],
                        ["ce_e"],
                    ),
                ],
            )
        )
        assert parsed.base_updated_at == UPDATED_AT
        assert [item.op for item in parsed.operations] == [
            "keep",
            "refine",
            "replace",
            "remove",
            "migrate_legacy",
        ]
        migrate = parsed.operations[-1]
        assert migrate.section == ""
        assert dict(migrate.sections)["今の関係"] == "我们是恋人。"


class TestBindingAndLimits:
    def test_evidence_must_belong_to_referenced_candidates(self, tmp_path: Path) -> None:
        _write_formal(tmp_path)
        maintainer, client, queue, _, store = _harness(tmp_path)
        left = queue.ingest(SCOPE, _explicit())
        right = queue.ingest(
            SCOPE,
            _explicit(subject_key="relationship.address", claim="我叫他你。", batch_id="curation_2"),
        )
        client.response = _proposal(
            UPDATED_AT,
            [_op("refine", "今の関係", "我们是恋人，也是同伴。", [left.id], [right.evidence[0].id])],
        )
        result = maintainer.run_once(SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1"))
        assert result.status == "rejected"
        assert queue.candidates_for(SCOPE)[0].status == "pending"
        assert isinstance(store, TrackingStore)
        assert store.patch_calls == []

    def test_section_must_match_candidate_target(self, tmp_path: Path) -> None:
        _write_formal(tmp_path)
        maintainer, client, queue, _, _ = _harness(tmp_path)
        candidate = queue.ingest(SCOPE, _explicit())
        client.response = _proposal(
            UPDATED_AT,
            [_op("refine", "今の私", "我变了。", [candidate.id], [candidate.evidence[0].id])],
        )
        result = maintainer.run_once(SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1"))
        assert result.status == "rejected"

    def test_ordinary_non_keep_max_two(self, tmp_path: Path) -> None:
        _write_formal(tmp_path)
        maintainer, client, queue, _, store = _harness(tmp_path)
        first = queue.ingest(SCOPE, _explicit())
        second = _ingest_observed(queue, subject_key="relationship.trust")
        third = _ingest_observed(queue, subject_key="relationship.habit")
        client.response = _proposal(
            UPDATED_AT,
            [
                _op("refine", first.target_section, "我们是恋人。", [first.id], [first.evidence[0].id]),
                _op("refine", second.target_section, "我更愿意说出来。", [second.id], [second.evidence[0].id]),
                _op("replace", third.target_section, "相处节奏变慢了。", [third.id], [third.evidence[0].id]),
            ],
        )
        result = maintainer.run_once(SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1"))
        assert result.status == "rejected"
        assert isinstance(store, TrackingStore)
        assert store.patch_calls == []

    def test_migration_plus_one_ordinary_change_is_one_patch(self, tmp_path: Path) -> None:
        _write_profile(
            tmp_path,
            _formal_v2({"legacy": "我们是恋人。\n我愿意把不安说出来。"}, content="我们是恋人。\n我愿意把不安说出来。"),
        )
        maintainer, client, queue, _, store = _harness(tmp_path)
        candidate = queue.ingest(SCOPE, _explicit())
        client.response = _proposal(
            UPDATED_AT,
            [
                _migrate_op(
                    {"今の関係": "我们是恋人。", "今の私": "我愿意把不安说出来。"},
                    [candidate.id],
                    [candidate.evidence[0].id],
                ),
                _op("refine", "今の関係", "我们确认了恋人关系。", [candidate.id], [candidate.evidence[0].id]),
            ],
        )
        result = maintainer.run_once(SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1"))
        assert result.status == "applied"
        assert result.metrics.get("ordinary_deferred") is True
        assert result.metrics["ops"]["migrate_legacy"] == 1
        assert result.metrics["ops"]["refine"] == 0
        assert isinstance(store, TrackingStore)
        assert len(store.patch_calls) == 1
        _args, kwargs = store.patch_calls[0]
        assert kwargs.get("migrate_legacy") is True
        saved = _saved_record(tmp_path)
        assert "legacy" not in saved["sections"]
        assert saved["sections"]["今の関係"] == "我们是恋人。"
        assert saved["sections"]["今の私"] == "我愿意把不安说出来。"
        assert candidate.id not in saved["metadata"].get("candidate_ids", [])
        assert queue.candidates_for(SCOPE)[0].status == "pending"

    def test_duplicate_non_keep_section_is_rejected(self, tmp_path: Path) -> None:
        _write_formal(tmp_path)
        maintainer, client, queue, _, store = _harness(tmp_path)
        first = queue.ingest(SCOPE, _explicit())
        second = queue.ingest(
            SCOPE,
            _explicit(
                subject_key="relationship.address",
                claim="我叫他你。",
                batch_id="curation_2",
            ),
        )
        client.response = _proposal(
            UPDATED_AT,
            [
                _op("refine", "今の関係", "我们是恋人。他叫我「小樱」。", [first.id], [first.evidence[0].id]),
                _op("replace", "今の関係", "我们确认了恋人关系。他叫我「小樱」。", [second.id], [second.evidence[0].id]),
            ],
        )
        result = maintainer.run_once(SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1"))
        assert result.status == "rejected"
        assert result.metrics["validation_rejected"] == "duplicate_section"
        assert isinstance(store, TrackingStore)
        assert store.patch_calls == []

    def test_candidate_cannot_be_both_keep_and_applied(self, tmp_path: Path) -> None:
        _write_formal(tmp_path)
        maintainer, client, queue, _, store = _harness(tmp_path)
        candidate = queue.ingest(SCOPE, _explicit())
        client.response = _proposal(
            UPDATED_AT,
            [
                _keep_op(candidate.id, candidate.evidence[0].id),
                _op(
                    "refine",
                    "今の関係",
                    "我们确认了恋人关系。他叫我「小樱」。",
                    [candidate.id],
                    [candidate.evidence[0].id],
                ),
            ],
        )
        result = maintainer.run_once(SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1"))
        assert result.status == "rejected"
        assert result.metrics["validation_rejected"] == "duplicate_candidate"
        assert isinstance(store, TrackingStore)
        assert store.patch_calls == []
        assert queue.candidates_for(SCOPE)[0].status == "pending"


class TestDeterministicValidation:
    def test_normalized_noop_becomes_keep(self, tmp_path: Path) -> None:
        _write_profile(tmp_path, _formal_v2({"今の関係": "我们是恋人。"}))
        maintainer, client, queue, _, store = _harness(tmp_path)
        candidate = queue.ingest(SCOPE, _explicit())
        client.response = _proposal(
            UPDATED_AT,
            [_op("refine", "今の関係", "  我们是恋人。 \n", [candidate.id], [candidate.evidence[0].id])],
        )
        result = maintainer.run_once(SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1"))
        assert result.status == "keep"
        assert isinstance(store, TrackingStore)
        assert store.patch_calls == []
        assert queue.candidates_for(SCOPE)[0].status == "reviewed"
        assert _saved_record(tmp_path)["metadata"]["updated_at"] == UPDATED_AT

    def test_shrink_over_40_percent_is_rejected(self, tmp_path: Path) -> None:
        long_body = "我们是恋人。" + ("我珍惜这段关系。" * 20)
        _write_profile(tmp_path, _formal_v2({"今の関係": long_body}))
        maintainer, client, queue, _, store = _harness(tmp_path)
        candidate = queue.ingest(SCOPE, _explicit())
        client.response = _proposal(
            UPDATED_AT,
            [_op("replace", "今の関係", "我们是恋人。", [candidate.id], [candidate.evidence[0].id])],
        )
        result = maintainer.run_once(SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1"))
        assert result.status == "rejected"
        assert result.metrics["validation_rejected"] == "shrink"
        assert isinstance(store, TrackingStore)
        assert store.patch_calls == []

    def test_protected_anchor_cannot_disappear_without_correction(self, tmp_path: Path) -> None:
        _write_profile(
            tmp_path,
            _formal_v2(
                {
                    "今の関係": (
                        "我们是恋人。他叫我「小樱」。"
                        "我珍惜这段稳定关系，也愿意继续把不安说出来。"
                    )
                }
            ),
        )
        maintainer, client, queue, _, _ = _harness(tmp_path)
        candidate = queue.ingest(SCOPE, _explicit())
        client.response = _proposal(
            UPDATED_AT,
            [
                _op(
                    "refine",
                    "今の関係",
                    "我们是恋人。我珍惜这段稳定关系，也愿意继续把不安说出来。",
                    [candidate.id],
                    [candidate.evidence[0].id],
                )
            ],
        )
        result = maintainer.run_once(SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1"))
        assert result.status == "rejected"
        assert result.metrics["validation_rejected"] == "anchors"

    def test_unquoted_name_and_identity_are_protected(self, tmp_path: Path) -> None:
        _write_profile(
            tmp_path,
            _formal_v2({"今の関係": "我们是恋人。他叫我小樱。我珍惜这段稳定关系。"}),
        )
        maintainer, client, queue, _, _ = _harness(tmp_path)
        candidate = queue.ingest(SCOPE, _explicit())
        client.response = _proposal(
            UPDATED_AT,
            [
                _op(
                    "refine",
                    "今の関係",
                    "我们确认了关系。我珍惜这段稳定关系。",
                    [candidate.id],
                    [candidate.evidence[0].id],
                )
            ],
        )
        result = maintainer.run_once(SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1"))
        assert result.status == "rejected"
        assert result.metrics["validation_rejected"] == "anchors"

    def test_correction_bypass_is_section_bound(self, tmp_path: Path) -> None:
        _write_profile(
            tmp_path,
            _formal_v2(
                {
                    "今の関係": "我们是恋人。他叫我小樱。我珍惜这段稳定关系。",
                    "今の私": "我愿意把不安说出来。",
                }
            ),
        )
        maintainer, client, queue, _, _ = _harness(tmp_path)
        identity = queue.ingest(SCOPE, _explicit())
        correction = queue.ingest(
            SCOPE,
            _explicit(
                target_section="今の私",
                subject_key="relationship.correction.mood",
                claim="那句自我认识需要改。",
                user_excerpt="那句关于不安的话改一下。",
                assistant_excerpt="好，我改那句。",
                batch_id="curation_fix_me",
            ),
        )
        client.response = _proposal(
            UPDATED_AT,
            [
                _op(
                    "refine",
                    "今の関係",
                    "我们确认了关系。我珍惜这段稳定关系。",
                    [identity.id],
                    [identity.evidence[0].id],
                ),
                _op(
                    "refine",
                    "今の私",
                    "我更愿意把不安说出来。",
                    [correction.id],
                    [correction.evidence[0].id],
                ),
            ],
        )
        result = maintainer.run_once(SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1"))
        assert result.status == "rejected"
        assert result.metrics["validation_rejected"] == "anchors"

    def test_section_bound_correction_can_drop_relationship_anchors(self, tmp_path: Path) -> None:
        _write_profile(
            tmp_path,
            _formal_v2({"今の関係": "我们是恋人。他叫我小樱。我珍惜这段稳定关系。"}),
        )
        maintainer, client, queue, _, _ = _harness(tmp_path)
        correction = queue.ingest(
            SCOPE,
            _explicit(
                subject_key="relationship.correction.identity",
                claim="称呼和关系确认都要改。",
                user_excerpt="别再叫小樱，也先不说恋人。",
                assistant_excerpt="好，那两处我改。",
                batch_id="curation_fix_rel",
            ),
        )
        client.response = _proposal(
            UPDATED_AT,
            [
                _op(
                    "refine",
                    "今の関係",
                    "我们确认了关系。我珍惜这段稳定关系。",
                    [correction.id],
                    [correction.evidence[0].id],
                )
            ],
        )
        result = maintainer.run_once(SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_fix_rel"))
        assert result.status == "applied"

    def test_agreement_tokens_are_protected_without_bound_correction(self, tmp_path: Path) -> None:
        _write_profile(
            tmp_path,
            _formal_v2({"今の関係": "我们是恋人。", "大切な約束と境界": "晚上十一点后不打电话。"}),
        )
        maintainer, client, queue, _, _ = _harness(tmp_path)
        candidate = queue.ingest(
            SCOPE,
            _explicit(
                target_section="大切な約束と境界",
                subject_key="relationship.agreement",
                claim="约定改成保持联系。",
                user_excerpt="晚上那条约定改成保持联系吧。",
                assistant_excerpt="好，改成保持联系。",
                batch_id="curation_agree",
            ),
        )
        client.response = _proposal(
            UPDATED_AT,
            [_op("refine", "大切な約束と境界", "保持联系。", [candidate.id], [candidate.evidence[0].id])],
        )
        result = maintainer.run_once(SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_agree"))
        assert result.status == "rejected"
        assert result.metrics["validation_rejected"] == "anchors"

    def test_remove_requires_explicit_correction(self, tmp_path: Path) -> None:
        _write_profile(tmp_path, _formal_v2({"今の関係": "我们是恋人。", "大切な約束と境界": "晚上十一点后不打电话。"}))
        maintainer, client, queue, _, _ = _harness(tmp_path)
        observed = _ingest_observed(queue, subject_key="relationship.habit", target_section="大切な約束と境界")
        client.response = _proposal(
            UPDATED_AT,
            [_op("remove", "大切な約束と境界", "", [observed.id], [observed.evidence[0].id])],
        )
        result = maintainer.run_once(
            SCOPE, MaintainerTrigger(kind="observed", candidate_id=observed.id)
        )
        assert result.status == "rejected"
        assert result.metrics["validation_rejected"] == "remove_requires_correction"

        correction = queue.ingest(
            SCOPE,
            _explicit(
                target_section="大切な約束と境界",
                subject_key="relationship.correction.phone",
                claim="那个约定已经取消了。",
                user_excerpt="晚上打电话的约定取消吧。",
                assistant_excerpt="好，那条约定取消。",
                batch_id="curation_fix",
            ),
        )
        client.response = _proposal(
            UPDATED_AT,
            [_op("remove", "大切な約束と境界", "", [correction.id], [correction.evidence[0].id])],
        )
        allowed = maintainer.run_once(
            SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_fix", candidate_id=correction.id)
        )
        assert allowed.status == "applied"
        assert _saved_record(tmp_path)["sections"]["大切な約束と境界"] == ""

    def test_system_report_language_is_rejected(self, tmp_path: Path) -> None:
        _write_formal(tmp_path)
        maintainer, client, queue, _, _ = _harness(tmp_path)
        candidate = queue.ingest(SCOPE, _explicit())
        client.response = _proposal(
            UPDATED_AT,
            [_op("refine", "今の関係", "用户画像显示我们应该扮演恋人。", [candidate.id], [candidate.evidence[0].id])],
        )
        result = maintainer.run_once(SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1"))
        assert result.status == "rejected"
        assert result.metrics["validation_rejected"] == "language"


class TestFailureCategories:
    def test_api_timeout_does_not_increase_streak(self, tmp_path: Path) -> None:
        _write_formal(tmp_path)
        settings = CoreMaintainerSettings(pause_after_validation_failures=1)
        maintainer, client, queue, _, _ = _harness(
            tmp_path, settings=settings, response=TimeoutError("timed out")
        )
        candidate = queue.ingest(SCOPE, _explicit())
        result = maintainer.run_once(SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1"))
        assert result.status == "api_error"
        assert queue.candidates_for(SCOPE)[0].status == "pending"
        client.response = _proposal(UPDATED_AT, [_keep_op(candidate.id, candidate.evidence[0].id)])
        later = maintainer.run_once(
            SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_2", candidate_id=candidate.id)
        )
        assert later.status == "keep"

    def test_three_validation_failures_pause_24h_and_keep_resets(self, tmp_path: Path) -> None:
        _write_formal(tmp_path)
        clock = Clock(NOW)
        settings = CoreMaintainerSettings(
            pause_after_validation_failures=3,
            pause_hours=24,
            normal_cooldown_hours=0,
        )
        maintainer, client, queue, _, _ = _harness(tmp_path, now=clock, settings=settings)
        candidate = queue.ingest(SCOPE, _explicit())
        client.response = "{bad"
        for _ in range(3):
            result = maintainer.run_once(
                SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1", candidate_id=candidate.id)
            )
            assert result.status == "rejected"
        paused = maintainer.run_once(
            SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1", candidate_id=candidate.id)
        )
        assert paused.status == "skipped_paused"
        clock.now = NOW + timedelta(hours=24)
        client.response = _proposal(UPDATED_AT, [_keep_op(candidate.id, candidate.evidence[0].id)])
        reset = maintainer.run_once(
            SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1", candidate_id=candidate.id)
        )
        assert reset.status == "keep"
        fresh = queue.ingest(
            SCOPE,
            _explicit(
                subject_key="relationship.boundary",
                claim="晚上十一点后不打电话。",
                batch_id="curation_batch_3",
                observed_at=clock.now.isoformat(),
            ),
        )
        client.response = "{bad"
        clock.now = NOW + timedelta(hours=30)
        again = maintainer.run_once(
            SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_3", candidate_id=fresh.id)
        )
        assert again.status == "rejected"
        still = maintainer.run_once(
            SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_4", candidate_id=fresh.id)
        )
        assert still.status == "rejected"

    def test_storage_failure_does_not_count(self, tmp_path: Path) -> None:
        _write_profile(tmp_path, _formal_v2({"今の関係": "我们是恋人。"}))
        settings = CoreMaintainerSettings(pause_after_validation_failures=1)
        store = TrackingStore(base_dir=tmp_path, scope_id=SCOPE, memory_client=object())

        def boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise CoreProfileStorageError("disk")

        store.patch_core_profile_sections = boom  # type: ignore[method-assign]
        maintainer, client, queue, _, _ = _harness(tmp_path, settings=settings, store=store)
        candidate = queue.ingest(SCOPE, _explicit())
        client.response = _proposal(
            UPDATED_AT,
            [_op("refine", "今の関係", "我们确认了恋人关系。", [candidate.id], [candidate.evidence[0].id])],
        )
        result = maintainer.run_once(SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1"))
        assert result.status == "storage_error"
        assert queue.candidates_for(SCOPE)[0].status == "pending"
        client.response = _proposal(UPDATED_AT, [_keep_op(candidate.id, candidate.evidence[0].id)])
        later = maintainer.run_once(
            SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_2", candidate_id=candidate.id)
        )
        assert later.status == "keep"


class TestQueueAndStateResilience:
    def test_partial_commit_then_repair_without_rewriting_core(self, tmp_path: Path) -> None:
        _write_profile(tmp_path, _formal_v2({"今の関係": "我们是恋人。"}))
        writes = {"count": 0}

        def writer(path: Path, text: str) -> None:
            writes["count"] += 1
            if writes["count"] > 1:
                raise OSError("queue write failed")
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(text, encoding="utf-8")

        maintainer, client, queue, _, store = _harness(tmp_path, queue_writer=writer)
        candidate = queue.ingest(SCOPE, _explicit())
        client.response = _proposal(
            UPDATED_AT,
            [_op("refine", "今の関係", "我们确认了恋人关系。", [candidate.id], [candidate.evidence[0].id])],
        )
        first = maintainer.run_once(SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1"))
        assert first.status == "partial_commit"
        saved = _saved_record(tmp_path)
        assert saved["sections"]["今の関係"] == "我们确认了恋人关系。"
        assert candidate.id in saved["metadata"]["candidate_ids"]
        assert queue.candidates_for(SCOPE)[0].status == "pending"

        maintainer.queue = CoreCandidateQueue(_queue_path(tmp_path), clock=lambda: NOW)
        repaired = maintainer.run_once(SCOPE)
        assert repaired.status == "recovered"
        assert queue.candidates_for(SCOPE)[0].status == "applied"
        assert _saved_record(tmp_path)["metadata"]["updated_at"] == saved["metadata"]["updated_at"]
        assert isinstance(store, TrackingStore)
        assert len(store.patch_calls) == 1
        assert len(client.calls) == 1

    def test_partial_repair_ignores_new_evidence_generation(self, tmp_path: Path) -> None:
        _write_profile(tmp_path, _formal_v2({"今の関係": "我们是恋人。"}))
        writes = {"count": 0}

        def writer(path: Path, text: str) -> None:
            writes["count"] += 1
            if writes["count"] > 1:
                raise OSError("queue write failed")
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(text, encoding="utf-8")

        clock = Clock(NOW)
        maintainer, client, queue, _, store = _harness(tmp_path, now=clock, queue_writer=writer)
        candidate = queue.ingest(SCOPE, _explicit())
        client.response = _proposal(
            UPDATED_AT,
            [_op("refine", "今の関係", "我们确认了恋人关系。", [candidate.id], [candidate.evidence[0].id])],
        )
        first = maintainer.run_once(SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1"))
        assert first.status == "partial_commit"
        assert queue.candidates_for(SCOPE)[0].status == "pending"

        fresh = CoreCandidateQueue(_queue_path(tmp_path), clock=clock)
        merged = fresh.ingest(
            SCOPE,
            _explicit(
                user_excerpt="后来又确认了一次。",
                assistant_excerpt="嗯，还是恋人。",
                batch_id="curation_batch_2",
                observed_at=(NOW + timedelta(minutes=1)).isoformat(),
            ),
        )
        assert merged.id == candidate.id
        assert len(merged.evidence) == 2
        maintainer.queue = fresh
        maintainer.scheduler.queue = fresh
        client.response = _proposal(UPDATED_AT, [_keep_op(merged.id, merged.evidence[-1].id)])
        second = maintainer.run_once(
            SCOPE,
            MaintainerTrigger(kind="explicit", batch_id="curation_batch_2", candidate_id=merged.id),
        )
        assert second.status != "recovered"
        assert fresh.candidates_for(SCOPE)[0].status != "applied"
        assert isinstance(store, TrackingStore)
        assert len(store.patch_calls) == 1
        assert len(client.calls) == 2

    def test_corrupt_queue_fails_closed(self, tmp_path: Path) -> None:
        _write_formal(tmp_path)
        maintainer, client, queue, _, _ = _harness(tmp_path)
        queue.ingest(SCOPE, _explicit())
        path = _queue_path(tmp_path)
        path.write_text("{broken", encoding="utf-8")
        before = path.read_bytes()
        with pytest.raises(CoreCandidateQueueError):
            maintainer.run_once(SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1"))
        assert path.read_bytes() == before
        assert client.calls == []

    def test_corrupt_state_fails_closed_without_overwrite(self, tmp_path: Path) -> None:
        _write_formal(tmp_path)
        path = _state_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")
        before = path.read_bytes()
        maintainer, client, queue, _, _ = _harness(tmp_path)
        queue.ingest(SCOPE, _explicit())
        with pytest.raises(CoreMaintainerStateError):
            maintainer.run_once(SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1"))
        assert path.read_bytes() == before
        assert client.calls == []


class TestMetadataOnlyMetrics:
    def test_metrics_omit_profile_claim_excerpt_prompt_and_reason(self, tmp_path: Path) -> None:
        _write_formal(tmp_path)
        maintainer, client, queue, _, _ = _harness(tmp_path)
        candidate = queue.ingest(
            SCOPE,
            _explicit(claim=SECRET_CLAIM, user_excerpt=SECRET_EXCERPT, assistant_excerpt="嗯，是恋人。"),
        )
        client.response = _proposal(
            UPDATED_AT,
            [
                _op(
                    "refine",
                    "今の関係",
                    "我们确认了恋人关系。他叫我「小樱」。",
                    [candidate.id],
                    [candidate.evidence[0].id],
                    reason=SECRET_REASON,
                )
            ],
        )
        result = maintainer.run_once(SCOPE, MaintainerTrigger(kind="explicit", batch_id="curation_batch_1"))
        assert result.status == "applied"
        payload = json.dumps(result.metrics, ensure_ascii=False)
        for secret in (SECRET_CLAIM, SECRET_EXCERPT, SECRET_PROFILE, SECRET_REASON, SECRET_MOOD):
            assert secret not in payload
        assert "prompt" not in result.metrics
        assert "raw_response" not in result.metrics
        assert "reason" not in result.metrics
        assert result.metrics["input_tokens"] is None
        assert result.metrics["output_tokens"] is None
        assert result.metrics["candidate_count"] == 1
        assert result.metrics["ops"]["refine"] == 1
