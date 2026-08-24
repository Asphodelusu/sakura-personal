"""tests/unit/test_core_profile_candidates.py — 常驻档案候选队列确定性单测。"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.agent.core_profile_candidates import (
    CoreCandidateConfig,
    CoreCandidateQueue,
    CoreCandidateQueueError,
    candidate_id,
    evidence_id,
    is_eligible,
)
from app.storage.paths import StoragePaths

NOW = datetime(2026, 8, 24, 10, 0, 0, tzinfo=timezone.utc)
FORMAL_SECTIONS = (
    "今の関係",
    "あなたについて知っていること",
    "今の私",
    "大切な約束と境界",
)
EXPLICIT_SUBJECTS = (
    "relationship.identity",
    "relationship.address",
    "relationship.agreement",
    "relationship.boundary",
    "relationship.correction",
)


def _queue_path(tmp_path: Path) -> Path:
    return StoragePaths(tmp_path).memory_core_review_queue()


def _queue(
    tmp_path: Path,
    *,
    now: datetime = NOW,
    config: CoreCandidateConfig | None = None,
    writer=None,
) -> CoreCandidateQueue:
    return CoreCandidateQueue(
        _queue_path(tmp_path),
        clock=lambda: now,
        config=config,
        writer=writer,
    )


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
    count: int,
    batches: int,
    span_minutes: float,
    confidence: float | list[float],
    scope_id: str = "Sakura",
    subject_key: str = "relationship.trust",
    start: datetime | None = None,
) -> Any:
    origin = start or (NOW - timedelta(minutes=span_minutes))
    last = None
    for index in range(count):
        if count == 1:
            observed_at = origin
        else:
            observed_at = origin + timedelta(minutes=span_minutes) * index / (count - 1)
        if isinstance(confidence, list):
            item_confidence = confidence[index]
        else:
            item_confidence = confidence
        last = queue.ingest(
            scope_id,
            _observed(
                subject_key=subject_key,
                user_excerpt=f"用户证据{index}",
                assistant_excerpt=f"我的回应{index}",
                batch_id=f"curation_{index % batches}",
                confidence=item_confidence,
                observed_at=observed_at.isoformat(),
            ),
        )
    return last


class TestStoragePath:
    def test_core_review_queue_maps_under_memory_dir(self, tmp_path: Path) -> None:
        paths = StoragePaths(tmp_path)
        assert paths.memory_core_review_queue() == tmp_path / "data" / "memory" / "core_review_queue.json"


class TestExplicitEligibility:
    def test_bilateral_high_confidence_supported_subject_is_eligible(
        self, tmp_path: Path
    ) -> None:
        queue = _queue(tmp_path)
        candidate = queue.ingest("Sakura", _explicit())
        assert candidate.kind == "explicit"
        assert is_eligible(candidate, now=NOW) is True
        assert [item.id for item in queue.eligible_for("Sakura")] == [candidate.id]

    @pytest.mark.parametrize("section", FORMAL_SECTIONS)
    def test_formal_sections_are_accepted(self, tmp_path: Path, section: str) -> None:
        queue = _queue(tmp_path)
        candidate = queue.ingest(
            "Sakura",
            _explicit(target_section=section, subject_key=f"relationship.identity.{section}"),
        )
        assert candidate.target_section == section
        assert is_eligible(candidate, now=NOW) is True

    @pytest.mark.parametrize("subject_key", EXPLICIT_SUBJECTS)
    def test_supported_explicit_categories_are_eligible(
        self, tmp_path: Path, subject_key: str
    ) -> None:
        queue = _queue(tmp_path)
        candidate = queue.ingest("Sakura", _explicit(subject_key=subject_key))
        assert is_eligible(candidate, now=NOW) is True

    def test_nested_supported_subject_is_eligible(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        candidate = queue.ingest(
            "Sakura",
            _explicit(subject_key="relationship.identity.lovers"),
        )
        assert is_eligible(candidate, now=NOW) is True

    def test_missing_user_excerpt_is_not_eligible(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        candidate = queue.ingest("Sakura", _explicit(user_excerpt=""))
        assert candidate.status == "pending"
        assert is_eligible(candidate, now=NOW) is False
        assert queue.eligible_for("Sakura") == ()

    def test_missing_assistant_excerpt_is_not_eligible(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        candidate = queue.ingest("Sakura", _explicit(assistant_excerpt="   "))
        assert is_eligible(candidate, now=NOW) is False

    def test_confidence_below_0_90_is_not_eligible(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        candidate = queue.ingest("Sakura", _explicit(confidence=0.89))
        assert is_eligible(candidate, now=NOW) is False

    def test_confidence_0_90_is_eligible(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        candidate = queue.ingest("Sakura", _explicit(confidence=0.90))
        assert is_eligible(candidate, now=NOW) is True

    def test_unsupported_subject_is_not_eligible(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        candidate = queue.ingest("Sakura", _explicit(subject_key="mood.jealousy"))
        assert is_eligible(candidate, now=NOW) is False

    def test_unknown_section_is_rejected(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        with pytest.raises(ValueError):
            queue.ingest("Sakura", _explicit(target_section="临时心情"))
        assert queue.candidates_for("Sakura") == ()

    def test_unknown_kind_is_rejected(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        with pytest.raises(ValueError):
            queue.ingest("Sakura", _explicit(kind="guess"))

    def test_injected_config_raises_explicit_confidence_bar(self, tmp_path: Path) -> None:
        queue = _queue(
            tmp_path,
            config=CoreCandidateConfig(explicit_min_confidence=0.97),
        )
        candidate = queue.ingest("Sakura", _explicit(confidence=0.95))
        assert is_eligible(candidate, now=NOW, config=queue.config) is False


class TestObservedEligibility:
    def test_meets_all_thresholds(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        candidate = _ingest_observed(
            queue, count=3, batches=2, span_minutes=30, confidence=0.80
        )
        assert candidate.kind == "observed"
        assert is_eligible(candidate, now=NOW) is True
        assert len(queue.eligible_for("Sakura")) == 1

    def test_two_evidence_is_not_eligible(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        candidate = _ingest_observed(
            queue, count=2, batches=2, span_minutes=30, confidence=0.85
        )
        assert len(candidate.evidence) == 2
        assert is_eligible(candidate, now=NOW) is False

    def test_single_batch_is_not_eligible(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        candidate = _ingest_observed(
            queue, count=3, batches=1, span_minutes=30, confidence=0.85
        )
        assert is_eligible(candidate, now=NOW) is False

    def test_span_29_minutes_is_not_eligible(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        candidate = _ingest_observed(
            queue, count=3, batches=2, span_minutes=29, confidence=0.85
        )
        assert is_eligible(candidate, now=NOW) is False

    def test_span_30_minutes_is_eligible(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        candidate = _ingest_observed(
            queue, count=3, batches=2, span_minutes=30, confidence=0.85
        )
        assert is_eligible(candidate, now=NOW) is True

    def test_average_confidence_0_79_is_not_eligible(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        candidate = _ingest_observed(
            queue, count=3, batches=2, span_minutes=30, confidence=0.79
        )
        assert candidate.confidence == pytest.approx(0.79)
        assert is_eligible(candidate, now=NOW) is False

    def test_average_confidence_mixed_0_80_is_eligible(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        candidate = _ingest_observed(
            queue,
            count=3,
            batches=2,
            span_minutes=30,
            confidence=[0.70, 0.80, 0.90],
        )
        assert candidate.confidence == pytest.approx(0.80)
        assert is_eligible(candidate, now=NOW) is True

    def test_mean_just_below_0_80_is_not_eligible(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        candidate = _ingest_observed(
            queue,
            count=3,
            batches=2,
            span_minutes=30,
            confidence=[0.80, 0.80, 0.79],
        )
        assert candidate.confidence < 0.80
        assert is_eligible(candidate, now=NOW) is False


class TestHashMergeClipAndCaps:
    def test_repeat_processing_is_idempotent(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        first = queue.ingest("Sakura", _explicit())
        second = queue.ingest("Sakura", _explicit())
        assert first.id == second.id
        stored = queue.candidates_for("Sakura")
        assert len(stored) == 1
        assert len(stored[0].evidence) == 1

    def test_normalized_excerpt_and_minute_bucket_share_evidence_hash(
        self, tmp_path: Path
    ) -> None:
        queue = _queue(tmp_path)
        queue.ingest(
            "Sakura",
            _explicit(
                user_excerpt="  我们是恋人吧。 ",
                observed_at="2026-08-24T10:00:11+00:00",
            ),
        )
        queue.ingest(
            "Sakura",
            _explicit(
                user_excerpt="我们是恋人吧。",
                observed_at="2026-08-24T10:00:59+00:00",
            ),
        )
        assert len(queue.candidates_for("Sakura")[0].evidence) == 1

    def test_different_batch_is_distinct_evidence(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        queue.ingest("Sakura", _explicit())
        queue.ingest("Sakura", _explicit(batch_id="curation_batch_2"))
        assert len(queue.candidates_for("Sakura")[0].evidence) == 2

    def test_same_subject_merges_instead_of_creating(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        first = queue.ingest("Sakura", _explicit())
        second = queue.ingest(
            "Sakura",
            _explicit(
                claim="我们再次确认了恋人关系。",
                user_excerpt="还是恋人。",
                assistant_excerpt="当然是。",
                batch_id="curation_batch_2",
            ),
        )
        stored = queue.candidates_for("Sakura")
        assert first.id == second.id
        assert len(stored) == 1
        assert stored[0].claim == "我们再次确认了恋人关系。"
        assert len(stored[0].evidence) == 2

    def test_different_section_or_subject_is_separate_candidate(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        left = queue.ingest("Sakura", _explicit())
        right = queue.ingest(
            "Sakura",
            _explicit(subject_key="relationship.address", claim="我称呼他为你。"),
        )
        ids = {item.id for item in queue.candidates_for("Sakura")}
        assert ids == {left.id, right.id}

    def test_caps_unique_evidence_at_five(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        candidate = _ingest_observed(
            queue, count=6, batches=6, span_minutes=60, confidence=0.85
        )
        assert len(candidate.evidence) == 5
        assert [item.user_excerpt for item in candidate.evidence] == [
            f"用户证据{index}" for index in range(1, 6)
        ]

    def test_clips_excerpt_and_claim(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        candidate = queue.ingest(
            "Sakura",
            _explicit(
                user_excerpt="你" * 161,
                assistant_excerpt="我" * 200,
                claim="认" * 241,
            ),
        )
        evidence = candidate.evidence[0]
        assert evidence.user_excerpt == "你" * 160
        assert evidence.assistant_excerpt == "我" * 160
        assert candidate.claim == "认" * 240

    def test_scopes_are_isolated(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        sakura = queue.ingest("Sakura", _explicit())
        katan = queue.ingest("Katan", _explicit(claim="另一段关系认识。"))
        assert [item.id for item in queue.candidates_for("Sakura")] == [sakura.id]
        assert [item.claim for item in queue.candidates_for("Katan")] == ["另一段关系认识。"]
        assert queue.eligible_for("Sakura")[0].claim != queue.eligible_for("Katan")[0].claim

    def test_prunes_to_50_candidates_per_scope(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        for index in range(51):
            observed_at = (NOW + timedelta(minutes=index)).isoformat()
            queue.ingest(
                "Sakura",
                _observed(
                    subject_key=f"relationship.habit.{index}",
                    user_excerpt=f"用户习惯{index}",
                    assistant_excerpt=f"我的观察{index}",
                    batch_id=f"curation_{index}",
                    observed_at=observed_at,
                ),
            )
        stored = queue.candidates_for("Sakura")
        assert len(stored) == 50
        assert "relationship.habit.0" not in {item.subject_key for item in stored}
        assert "relationship.habit.50" in {item.subject_key for item in stored}

    def test_other_scope_has_its_own_capacity(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        for index in range(50):
            queue.ingest(
                "Sakura",
                _observed(
                    subject_key=f"relationship.habit.{index}",
                    user_excerpt=f"用户习惯{index}",
                    assistant_excerpt=f"我的观察{index}",
                    batch_id=f"curation_{index}",
                    observed_at=(NOW + timedelta(minutes=index)).isoformat(),
                ),
            )
        extra = queue.ingest("Katan", _explicit())
        assert len(queue.candidates_for("Sakura")) == 50
        assert queue.candidates_for("Katan") == (extra,)

    def test_pending_expires_after_30_days(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        queue.ingest("Sakura", _explicit())
        later = _queue(tmp_path, now=NOW + timedelta(days=30))
        stored = later.candidates_for("Sakura")
        assert len(stored) == 1
        assert stored[0].status == "expired"
        assert later.eligible_for("Sakura") == ()
        persisted = json.loads(_queue_path(tmp_path).read_text(encoding="utf-8"))
        assert persisted["scopes"]["Sakura"]["candidates"][0]["status"] == "expired"

    def test_pending_not_expired_before_30_days(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        queue.ingest("Sakura", _explicit())
        later = _queue(tmp_path, now=NOW + timedelta(days=30) - timedelta(seconds=1))
        assert later.candidates_for("Sakura")[0].status == "pending"
        assert later.eligible_for("Sakura")

    def test_processed_dropped_after_7_days(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        candidate = queue.ingest("Sakura", _explicit())
        path = _queue_path(tmp_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["scopes"]["Sakura"]["candidates"][0]["status"] = "applied"
        payload["scopes"]["Sakura"]["candidates"][0]["last_seen_at"] = (
            NOW - timedelta(days=7)
        ).isoformat()
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        later = _queue(tmp_path, now=NOW)
        assert later.candidates_for("Sakura") == ()
        assert candidate.id not in {item.id for item in later.candidates_for("Sakura")}
        persisted = json.loads(path.read_text(encoding="utf-8"))
        assert persisted["scopes"]["Sakura"]["candidates"] == []

    def test_processed_kept_before_7_days(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        candidate = queue.ingest("Sakura", _explicit())
        path = _queue_path(tmp_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["scopes"]["Sakura"]["candidates"][0]["status"] = "reviewed"
        payload["scopes"]["Sakura"]["candidates"][0]["last_seen_at"] = (
            NOW - timedelta(days=7) + timedelta(seconds=1)
        ).isoformat()
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        later = _queue(tmp_path, now=NOW)
        stored = later.candidates_for("Sakura")
        assert len(stored) == 1
        assert stored[0].id == candidate.id
        assert stored[0].status == "reviewed"

    def test_unknown_status_on_disk_is_rejected(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        queue.ingest("Sakura", _explicit())
        path = _queue_path(tmp_path)
        original = path.read_bytes()
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["scopes"]["Sakura"]["candidates"][0]["status"] = "queued"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        corrupted = path.read_bytes()
        later = CoreCandidateQueue(path, clock=lambda: NOW)
        with pytest.raises(CoreCandidateQueueError):
            later.candidates_for("Sakura")
        assert path.read_bytes() == corrupted
        assert original != corrupted


class TestPersistence:
    def test_missing_file_is_empty_queue(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        assert queue.candidates_for("Sakura") == ()
        assert not _queue_path(tmp_path).exists()

    def test_reload_preserves_stable_ids(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        candidate = queue.ingest("Sakura", _explicit())
        reloaded = _queue(tmp_path)
        stored = reloaded.candidates_for("Sakura")
        assert stored[0].id == candidate.id
        assert stored[0].evidence[0].id == candidate.evidence[0].id
        assert stored[0].kind == "explicit"

    def test_malformed_json_raises_typed_error_and_keeps_bytes(self, tmp_path: Path) -> None:
        path = _queue_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{not-json")
        original = path.read_bytes()
        queue = CoreCandidateQueue(path, clock=lambda: NOW)
        with pytest.raises(CoreCandidateQueueError):
            queue.candidates_for("Sakura")
        assert path.read_bytes() == original
        with pytest.raises(CoreCandidateQueueError):
            queue.ingest("Sakura", _explicit())
        assert path.read_bytes() == original

    def test_top_level_non_object_raises_typed_error(self, tmp_path: Path) -> None:
        path = _queue_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[]", encoding="utf-8")
        original = path.read_bytes()
        queue = CoreCandidateQueue(path, clock=lambda: NOW)
        with pytest.raises(CoreCandidateQueueError):
            queue.candidates_for("Sakura")
        assert path.read_bytes() == original

    def test_failed_save_does_not_overwrite_source(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        queue.ingest("Sakura", _explicit())
        path = _queue_path(tmp_path)
        original = path.read_bytes()

        def fail_write(_path: Path, _text: str) -> None:
            raise OSError("disk full")

        later = CoreCandidateQueue(path, clock=lambda: NOW, writer=fail_write)
        with pytest.raises(OSError):
            later.ingest(
                "Sakura",
                _explicit(subject_key="relationship.address", claim="我改称呼了。"),
            )
        assert path.read_bytes() == original
        assert [item.subject_key for item in later.candidates_for("Sakura")] == [
            "relationship.identity"
        ]

    def test_successful_save_is_complete_json(self, tmp_path: Path) -> None:
        writes: list[str] = []

        def tracking_writer(path: Path, text: str) -> None:
            writes.append(text)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

        queue = _queue(tmp_path, writer=tracking_writer)
        queue.ingest("Sakura", _explicit())
        assert writes
        payload = json.loads(writes[-1])
        assert payload["schema_version"] == 1
        assert "Sakura" in payload["scopes"]
        saved = json.loads(_queue_path(tmp_path).read_text(encoding="utf-8"))
        assert saved == payload
        json.dumps(saved, allow_nan=False)


class TestReviewRegressions:
    def test_new_batch_is_retained_after_evidence_cap(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        for index in range(5):
            queue.ingest(
                "Sakura",
                _observed(
                    user_excerpt=f"同一批次{index}",
                    assistant_excerpt=f"回应{index}",
                    batch_id="curation_same",
                    observed_at=(NOW - timedelta(minutes=40) + timedelta(minutes=index)).isoformat(),
                ),
            )
        candidate = queue.ingest(
            "Sakura",
            _observed(
                user_excerpt="新批次证据",
                assistant_excerpt="新批次回应",
                batch_id="curation_new",
                observed_at=NOW.isoformat(),
            ),
        )
        batches = {item.batch_id for item in candidate.evidence}
        excerpts = [item.user_excerpt for item in candidate.evidence]
        assert len(candidate.evidence) == 5
        assert "curation_new" in batches
        assert len(batches) == 2
        assert "新批次证据" in excerpts
        assert "同一批次0" not in excerpts

    def test_explicit_upgrade_requires_retained_explicit_evidence(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        for index in range(5):
            queue.ingest(
                "Sakura",
                _observed(
                    target_section="今の関係",
                    subject_key="relationship.identity",
                    claim="还在观察关系。",
                    user_excerpt=f"观察{index}",
                    assistant_excerpt=f"回应{index}",
                    batch_id=f"curation_obs_{index}",
                    confidence=0.85,
                    observed_at=(NOW - timedelta(minutes=50) + timedelta(minutes=index)).isoformat(),
                ),
            )
        upgraded = queue.ingest(
            "Sakura",
            _explicit(
                claim="我们明确确认了恋人关系。",
                user_excerpt="我们是恋人吧。",
                assistant_excerpt="嗯，是恋人。",
                batch_id="curation_explicit",
                confidence=0.96,
                observed_at=NOW.isoformat(),
            ),
        )
        assert upgraded.kind == "explicit"
        assert upgraded.claim == "我们明确确认了恋人关系。"
        assert any(
            item.user_excerpt == "我们是恋人吧。" and item.kind == "explicit"
            for item in upgraded.evidence
        )
        assert is_eligible(upgraded, now=NOW) is True

    def test_dropped_explicit_does_not_upgrade_kind_or_claim(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path, config=CoreCandidateConfig(max_evidence=1))
        observed = queue.ingest(
            "Sakura",
            _observed(
                target_section="今の関係",
                subject_key="relationship.identity",
                claim="还在观察关系。",
                user_excerpt="先留下这条观察",
                assistant_excerpt="先记下",
                batch_id="curation_keep",
                observed_at=NOW.isoformat(),
            ),
        )
        # Same observed_at minute bucket as the retained evidence would collide if
        # excerpts matched; this explicit payload is unique but older, so a recency
        # cap should keep the original observed evidence.
        result = queue.ingest(
            "Sakura",
            _explicit(
                claim="我们明确确认了恋人关系。",
                user_excerpt="我们是恋人吧。",
                assistant_excerpt="嗯，是恋人。",
                batch_id="curation_old_explicit",
                confidence=0.96,
                observed_at=(NOW - timedelta(minutes=10)).isoformat(),
            ),
        )
        assert result.id == observed.id
        assert result.kind == "observed"
        assert result.claim == "还在观察关系。"
        assert [item.user_excerpt for item in result.evidence] == ["先留下这条观察"]

    def test_second_instance_reloads_under_shared_lock(self, tmp_path: Path) -> None:
        first = _queue(tmp_path)
        first.ingest("Sakura", _explicit())
        second = _queue(tmp_path)
        second.ingest(
            "Sakura",
            _explicit(
                subject_key="relationship.address",
                claim="我改称呼了。",
                user_excerpt="以后叫这个。",
                assistant_excerpt="好。",
                batch_id="curation_address",
            ),
        )
        first.ingest(
            "Sakura",
            _explicit(
                subject_key="relationship.agreement",
                claim="我们约定了边界。",
                user_excerpt="这是约定。",
                assistant_excerpt="记下了。",
                batch_id="curation_agreement",
            ),
        )
        keys = {item.subject_key for item in _queue(tmp_path).candidates_for("Sakura")}
        assert keys == {
            "relationship.identity",
            "relationship.address",
            "relationship.agreement",
        }

    def test_concurrent_ingests_do_not_drop_candidates(self, tmp_path: Path) -> None:
        path = _queue_path(tmp_path)
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                queue = CoreCandidateQueue(path, clock=lambda: NOW + timedelta(minutes=index))
                queue.ingest(
                    "Sakura",
                    _explicit(
                        subject_key=f"relationship.identity.{index}",
                        claim=f"确认{index}。",
                        user_excerpt=f"用户{index}",
                        assistant_excerpt=f"回应{index}",
                        batch_id=f"curation_{index}",
                        observed_at=(NOW + timedelta(minutes=index)).isoformat(),
                    ),
                )
            except BaseException as exc:  # noqa: BLE001 - collect any worker failure
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        stored = _queue(tmp_path).candidates_for("Sakura")
        assert len(stored) == 8

    def test_inconsistent_candidate_id_is_rejected(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        queue.ingest("Sakura", _explicit())
        path = _queue_path(tmp_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["scopes"]["Sakura"]["candidates"][0]["id"] = "cc_not-the-real-hash"
        corrupted = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        path.write_bytes(corrupted)
        later = CoreCandidateQueue(path, clock=lambda: NOW)
        with pytest.raises(CoreCandidateQueueError):
            later.candidates_for("Sakura")
        assert path.read_bytes() == corrupted

    def test_inconsistent_evidence_id_is_rejected(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        queue.ingest("Sakura", _explicit())
        path = _queue_path(tmp_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["scopes"]["Sakura"]["candidates"][0]["evidence"][0]["id"] = "ce_not-the-real-hash"
        corrupted = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        path.write_bytes(corrupted)
        later = CoreCandidateQueue(path, clock=lambda: NOW)
        with pytest.raises(CoreCandidateQueueError):
            later.candidates_for("Sakura")
        assert path.read_bytes() == corrupted

    def test_inconsistent_aggregate_confidence_is_rejected(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        queue.ingest("Sakura", _explicit(confidence=0.95))
        path = _queue_path(tmp_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["scopes"]["Sakura"]["candidates"][0]["confidence"] = 0.50
        corrupted = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        path.write_bytes(corrupted)
        later = CoreCandidateQueue(path, clock=lambda: NOW)
        with pytest.raises(CoreCandidateQueueError):
            later.candidates_for("Sakura")
        assert path.read_bytes() == corrupted

    def test_duplicate_evidence_on_disk_is_rejected(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        queue.ingest("Sakura", _explicit())
        path = _queue_path(tmp_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        evidence = payload["scopes"]["Sakura"]["candidates"][0]["evidence"][0]
        payload["scopes"]["Sakura"]["candidates"][0]["evidence"] = [evidence, dict(evidence)]
        corrupted = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        path.write_bytes(corrupted)
        later = CoreCandidateQueue(path, clock=lambda: NOW)
        with pytest.raises(CoreCandidateQueueError):
            later.candidates_for("Sakura")
        assert path.read_bytes() == corrupted

    def test_over_cap_evidence_on_disk_is_rejected(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        queue.ingest("Sakura", _explicit())
        path = _queue_path(tmp_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        evidence_items = []
        for index in range(6):
            user_excerpt = f"用户{index}"
            assistant_excerpt = f"回应{index}"
            batch_id = f"curation_{index}"
            observed_at = (NOW + timedelta(minutes=index)).isoformat()
            evidence_items.append(
                {
                    "id": evidence_id(
                        user_excerpt=user_excerpt,
                        assistant_excerpt=assistant_excerpt,
                        observed_at=observed_at,
                        batch_id=batch_id,
                    ),
                    "user_excerpt": user_excerpt,
                    "assistant_excerpt": assistant_excerpt,
                    "observed_at": observed_at,
                    "batch_id": batch_id,
                    "confidence": 0.85,
                    "kind": "observed",
                }
            )
        candidate = payload["scopes"]["Sakura"]["candidates"][0]
        candidate["evidence"] = evidence_items
        candidate["kind"] = "observed"
        candidate["id"] = candidate_id(candidate["target_section"], candidate["subject_key"])
        candidate["confidence"] = 0.85
        corrupted = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        path.write_bytes(corrupted)
        later = CoreCandidateQueue(path, clock=lambda: NOW)
        with pytest.raises(CoreCandidateQueueError):
            later.candidates_for("Sakura")
        assert path.read_bytes() == corrupted

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_confidence_is_rejected(self, tmp_path: Path, value: float) -> None:
        queue = _queue(tmp_path)
        with pytest.raises(ValueError):
            queue.ingest("Sakura", _explicit(confidence=value))
        assert not _queue_path(tmp_path).exists()

    def test_non_finite_confidence_on_disk_is_rejected(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path)
        queue.ingest("Sakura", _explicit())
        path = _queue_path(tmp_path)
        original = path.read_bytes()
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["scopes"]["Sakura"]["candidates"][0]["confidence"] = float("nan")
        path.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=True), encoding="utf-8")
        corrupted = path.read_bytes()
        later = CoreCandidateQueue(path, clock=lambda: NOW)
        with pytest.raises(CoreCandidateQueueError):
            later.candidates_for("Sakura")
        assert path.read_bytes() == corrupted
        assert original != corrupted

