"""常驻档案 maintainer：同步调度、严格解析、确定性校验与元数据指标。"""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.agent.core_profile_candidates import (
    CoreCandidate,
    CoreCandidateConfig,
    CoreCandidateQueue,
    candidate_generation_fingerprint,
    eligible_since,
    is_eligible,
    is_supported_explicit_subject,
    exclusive_json_path_lock,
)
from app.agent.memory import CORE_PROFILE_FORMAL_SECTIONS, CoreProfileStorageError
from app.agent.time_awareness import parse_iso_datetime
from app.storage.atomic import atomic_write_text

STATE_SCHEMA_VERSION = 1
ALLOWED_OPS = frozenset({"keep", "refine", "replace", "remove", "migrate_legacy"})
ROOT_KEYS = frozenset({"base_updated_at", "operations"})
KEEP_OPERATION_KEYS = frozenset({"op", "section", "reason", "candidate_ids", "evidence_ids"})
MUTATING_OPERATION_KEYS = frozenset(
    {"op", "section", "content", "reason", "candidate_ids", "evidence_ids"}
)
MIGRATE_OPERATION_KEYS = frozenset({"op", "sections", "reason", "candidate_ids", "evidence_ids"})
FORBIDDEN_LANGUAGE = (
    "用户画像",
    "系统设定",
    "应该扮演",
    "系统规则",
    "user profile",
    "该用户",
)
_QUOTED_TOKEN = re.compile("「[^」]*」|『[^』]*』|\"[^\"]*\"|'[^']*'")
_NUMBER_TOKEN = re.compile(r"\d+(?:\.\d+)?")
_ADDRESS_NAME = re.compile(r"(?:叫我|叫他|叫她|称呼(?:我|他|她)?)([\u4e00-\u9fffA-Za-z]{1,8})")
_IDENTITY_TERMS = ("恋人", "朋友", "伴侣", "夫妻", "家人")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
_AGREEMENT_STOP = frozenset({"我们", "他们", "自己", "这个", "那个", "一段", "一种"})
_FORMAL_SECTION_SET = frozenset(CORE_PROFILE_FORMAL_SECTIONS)


class CoreMaintainerStateError(RuntimeError):
    """维护器状态损坏；不得覆盖源文件。"""


class MaintainerParseError(ValueError):
    """模型输出不是严格 JSON 协议。"""


class MaintainerValidationError(ValueError):
    """确定性校验失败。"""

    def __init__(self, category: str, message: str = "") -> None:
        super().__init__(message or category)
        self.category = category


@dataclass(frozen=True)
class CoreMaintainerSettings:
    enabled: bool = True
    observed_min_evidence: int = 3
    observed_min_batches: int = 2
    observed_min_span_minutes: int = 30
    observed_min_confidence: float = 0.80
    normal_cooldown_hours: int = 6
    stale_eligible_hours: int = 72
    max_candidates_per_call: int = 5
    max_sections_per_call: int = 2
    pause_after_validation_failures: int = 3
    pause_hours: int = 24
    lease_ttl_minutes: int = 30

    def normalized(self) -> CoreMaintainerSettings:
        return CoreMaintainerSettings(
            enabled=bool(self.enabled),
            observed_min_evidence=_clamp_int(self.observed_min_evidence, 1, 10, 3),
            observed_min_batches=_clamp_int(self.observed_min_batches, 1, 10, 2),
            observed_min_span_minutes=_clamp_int(self.observed_min_span_minutes, 0, 24 * 60, 30),
            observed_min_confidence=_clamp_float(self.observed_min_confidence, 0.0, 1.0, 0.80),
            normal_cooldown_hours=_clamp_int(self.normal_cooldown_hours, 0, 168, 6),
            stale_eligible_hours=_clamp_int(self.stale_eligible_hours, 0, 24 * 30, 72),
            max_candidates_per_call=_clamp_int(self.max_candidates_per_call, 1, 5, 5),
            max_sections_per_call=_clamp_int(self.max_sections_per_call, 1, 2, 2),
            pause_after_validation_failures=_clamp_int(
                self.pause_after_validation_failures, 1, 20, 3
            ),
            pause_hours=_clamp_int(self.pause_hours, 1, 168, 24),
            lease_ttl_minutes=_clamp_int(self.lease_ttl_minutes, 1, 24 * 60, 30),
        )

    def to_candidate_config(self) -> CoreCandidateConfig:
        cfg = self.normalized()
        return CoreCandidateConfig(
            observed_min_evidence=cfg.observed_min_evidence,
            observed_min_batches=cfg.observed_min_batches,
            observed_min_span_minutes=cfg.observed_min_span_minutes,
            observed_min_confidence=cfg.observed_min_confidence,
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> CoreMaintainerSettings:
        source = dict(raw) if isinstance(raw, Mapping) else {}
        defaults = cls()
        return cls(
            enabled=_coerce_bool(source.get("enabled"), defaults.enabled),
            observed_min_evidence=_coerce_int(
                source.get("observed_min_evidence"), defaults.observed_min_evidence
            ),
            observed_min_batches=_coerce_int(
                source.get("observed_min_batches"), defaults.observed_min_batches
            ),
            observed_min_span_minutes=_coerce_int(
                source.get("observed_min_span_minutes"), defaults.observed_min_span_minutes
            ),
            observed_min_confidence=_coerce_float(
                source.get("observed_min_confidence"), defaults.observed_min_confidence
            ),
            normal_cooldown_hours=_coerce_int(
                source.get("normal_cooldown_hours"), defaults.normal_cooldown_hours
            ),
            stale_eligible_hours=_coerce_int(
                source.get("stale_eligible_hours"), defaults.stale_eligible_hours
            ),
            max_candidates_per_call=_coerce_int(
                source.get("max_candidates_per_call"), defaults.max_candidates_per_call
            ),
            max_sections_per_call=_coerce_int(
                source.get("max_sections_per_call"), defaults.max_sections_per_call
            ),
            pause_after_validation_failures=_coerce_int(
                source.get("pause_after_validation_failures"),
                defaults.pause_after_validation_failures,
            ),
            pause_hours=_coerce_int(source.get("pause_hours"), defaults.pause_hours),
            lease_ttl_minutes=_coerce_int(
                source.get("lease_ttl_minutes"), defaults.lease_ttl_minutes
            ),
        ).normalized()


@dataclass(frozen=True)
class MaintainerTrigger:
    kind: str
    batch_id: str = ""
    candidate_id: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "batch_id": self.batch_id,
            "candidate_id": self.candidate_id,
        }


@dataclass(frozen=True)
class ScheduleDecision:
    admitted: bool
    reason: str
    selected: tuple[CoreCandidate, ...] = ()
    lease_holder: str = ""
    used_explicit_bypass: bool = False


@dataclass(frozen=True)
class MaintainerOperation:
    op: str
    section: str
    content: str
    reason: str
    candidate_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    sections: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class MaintainerProposal:
    base_updated_at: str
    operations: tuple[MaintainerOperation, ...]


@dataclass(frozen=True)
class MaintainerRunResult:
    status: str
    metrics: dict[str, Any] = field(default_factory=dict)


def parse_maintainer_response(raw: str) -> MaintainerProposal:
    text = str(raw or "").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MaintainerParseError("invalid json") from exc
    if not isinstance(data, dict) or set(data.keys()) != ROOT_KEYS:
        raise MaintainerParseError("invalid keys")
    base_updated_at = data.get("base_updated_at")
    if not isinstance(base_updated_at, str) or not base_updated_at.strip():
        raise MaintainerParseError("invalid base_updated_at")
    operations_raw = data.get("operations")
    if not isinstance(operations_raw, list):
        raise MaintainerParseError("invalid operations")
    operations = tuple(_parse_operation(item) for item in operations_raw)
    return MaintainerProposal(base_updated_at=base_updated_at.strip(), operations=operations)


class CoreMaintainerStateStore:
    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        writer: Callable[[Path, str], None] | None = None,
    ) -> None:
        self._path = Path(path)
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._writer = writer or (lambda target, text: atomic_write_text(target, text))

    def try_acquire_lease(self, holder: str, *, ttl: timedelta | None = None) -> bool:
        limit = ttl if ttl is not None else timedelta(minutes=30)
        now = self._clock()
        with exclusive_json_path_lock(self._path):
            document = self._load_locked()
            lease = document.get("lease")
            if isinstance(lease, dict):
                current = str(lease.get("holder") or "").strip()
                if current and current != holder and _lease_is_active(lease, now, limit):
                    return False
            document["lease"] = {"holder": holder, "acquired_at": now.isoformat()}
            self._write_locked(document)
            return True

    def release_lease(self, holder: str) -> None:
        with exclusive_json_path_lock(self._path):
            document = self._load_locked()
            lease = document.get("lease")
            if isinstance(lease, dict) and str(lease.get("holder") or "").strip() == holder:
                document["lease"] = None
                self._write_locked(document)

    def is_paused(self, scope_id: str, now: datetime) -> bool:
        with exclusive_json_path_lock(self._path):
            paused_until = self._scope_locked(self._load_locked(), scope_id).get("paused_until")
        parsed = parse_iso_datetime(str(paused_until or ""))
        return parsed is not None and parsed > now

    def pending_triggers(self, scope_id: str) -> tuple[MaintainerTrigger, ...]:
        with exclusive_json_path_lock(self._path):
            raw = self._scope_locked(self._load_locked(), scope_id).get("pending_triggers") or []
        return tuple(_trigger_from_raw(item) for item in raw if _trigger_from_raw(item) is not None)

    def add_pending_trigger(self, scope_id: str, trigger: MaintainerTrigger) -> None:
        with exclusive_json_path_lock(self._path):
            document = self._load_locked()
            scope = self._scope_locked(document, scope_id)
            items = list(scope.get("pending_triggers") or [])
            payload = trigger.to_dict()
            if payload not in items:
                items.append(payload)
            scope["pending_triggers"] = items
            self._write_locked(document)

    def clear_pending_triggers(self, scope_id: str) -> None:
        self.replace_pending_triggers(scope_id, ())

    def replace_pending_triggers(self, scope_id: str, triggers: Sequence[MaintainerTrigger]) -> None:
        with exclusive_json_path_lock(self._path):
            document = self._load_locked()
            self._scope_locked(document, scope_id)["pending_triggers"] = [
                item.to_dict() for item in triggers
            ]
            self._write_locked(document)

    def last_invoked_at(self, scope_id: str) -> datetime | None:
        with exclusive_json_path_lock(self._path):
            raw = self._scope_locked(self._load_locked(), scope_id).get("last_invoked_at")
        return parse_iso_datetime(str(raw or ""))

    def mark_invoked(self, scope_id: str) -> None:
        with exclusive_json_path_lock(self._path):
            document = self._load_locked()
            self._scope_locked(document, scope_id)["last_invoked_at"] = self._clock().isoformat()
            self._write_locked(document)

    def record_explicit_batch(self, scope_id: str, batch_id: str) -> None:
        batch = str(batch_id or "").strip()
        if not batch:
            return
        with exclusive_json_path_lock(self._path):
            document = self._load_locked()
            scope = self._scope_locked(document, scope_id)
            used = [str(item) for item in (scope.get("explicit_bypass_batches") or [])]
            if batch not in used:
                used.append(batch)
            scope["explicit_bypass_batches"] = used
            self._write_locked(document)

    def explicit_batch_used(self, scope_id: str, batch_id: str) -> bool:
        batch = str(batch_id or "").strip()
        with exclusive_json_path_lock(self._path):
            used = self._scope_locked(self._load_locked(), scope_id).get("explicit_bypass_batches") or []
        return batch in {str(item) for item in used}

    def pending_repairs(self, scope_id: str) -> tuple[dict[str, str], ...]:
        with exclusive_json_path_lock(self._path):
            raw = self._scope_locked(self._load_locked(), scope_id).get("pending_repairs") or []
        records: list[dict[str, str]] = []
        if not isinstance(raw, list):
            return ()
        for item in raw:
            parsed = _repair_record_from_raw(item)
            if parsed is not None:
                records.append(parsed)
        return tuple(records)

    def matching_generation_repairs(
        self,
        scope_id: str,
        pending: Sequence[CoreCandidate],
    ) -> tuple[dict[str, str], ...]:
        by_id = {item.id: item for item in pending if item.status == "pending"}
        matched: list[dict[str, str]] = []
        for record in self.pending_repairs(scope_id):
            candidate = by_id.get(record["candidate_id"])
            if candidate is None:
                continue
            if candidate_generation_fingerprint(candidate) != record["evidence_fingerprint"]:
                continue
            matched.append(record)
        return tuple(matched)

    def record_pending_repairs(self, scope_id: str, records: Sequence[Mapping[str, str]]) -> None:
        incoming = [dict(item) for item in records if _repair_record_from_raw(item) is not None]
        if not incoming:
            return
        with exclusive_json_path_lock(self._path):
            document = self._load_locked()
            scope = self._scope_locked(document, scope_id)
            existing = [
                parsed
                for item in (scope.get("pending_repairs") or [])
                if (parsed := _repair_record_from_raw(item)) is not None
            ]
            by_id = {item["candidate_id"]: item for item in existing}
            for item in incoming:
                parsed = _repair_record_from_raw(item)
                if parsed is not None:
                    by_id[parsed["candidate_id"]] = parsed
            scope["pending_repairs"] = list(by_id.values())
            self._write_locked(document)

    def clear_pending_repairs(self, scope_id: str, candidate_ids: Sequence[str] | None = None) -> None:
        drop = {str(item).strip() for item in (candidate_ids or ()) if str(item).strip()}
        with exclusive_json_path_lock(self._path):
            document = self._load_locked()
            scope = self._scope_locked(document, scope_id)
            if not drop:
                scope["pending_repairs"] = []
            else:
                kept = [
                    parsed
                    for item in (scope.get("pending_repairs") or [])
                    if (parsed := _repair_record_from_raw(item)) is not None
                    and parsed["candidate_id"] not in drop
                ]
                scope["pending_repairs"] = kept
            self._write_locked(document)

    def record_validation_failure(self, scope_id: str, *, threshold: int, pause_hours: int) -> None:
        with exclusive_json_path_lock(self._path):
            document = self._load_locked()
            scope = self._scope_locked(document, scope_id)
            streak = int(scope.get("validation_failure_streak") or 0) + 1
            scope["validation_failure_streak"] = streak
            if streak >= threshold:
                scope["paused_until"] = (self._clock() + timedelta(hours=pause_hours)).isoformat()
            self._write_locked(document)

    def reset_validation_streak(self, scope_id: str) -> None:
        with exclusive_json_path_lock(self._path):
            document = self._load_locked()
            scope = self._scope_locked(document, scope_id)
            scope["validation_failure_streak"] = 0
            scope["paused_until"] = None
            self._write_locked(document)

    def _load_locked(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"schema_version": STATE_SCHEMA_VERSION, "lease": None, "scopes": {}}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise CoreMaintainerStateError("core maintainer state is malformed") from exc
        except OSError as exc:
            raise CoreMaintainerStateError("core maintainer state could not be read") from exc
        return self._parse_document(raw)

    def _parse_document(self, raw: object) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise CoreMaintainerStateError("core maintainer state is malformed")
        if not raw:
            return {"schema_version": STATE_SCHEMA_VERSION, "lease": None, "scopes": {}}
        version = raw.get("schema_version", STATE_SCHEMA_VERSION)
        if isinstance(version, bool) or version != STATE_SCHEMA_VERSION:
            raise CoreMaintainerStateError("core maintainer state is malformed")
        lease = raw.get("lease")
        if lease is not None and not isinstance(lease, dict):
            raise CoreMaintainerStateError("core maintainer state is malformed")
        scopes = raw.get("scopes", {})
        if not isinstance(scopes, dict):
            raise CoreMaintainerStateError("core maintainer state is malformed")
        parsed_scopes: dict[str, Any] = {}
        for scope_id, scope_raw in scopes.items():
            if not isinstance(scope_id, str) or not scope_id.strip() or not isinstance(scope_raw, dict):
                raise CoreMaintainerStateError("core maintainer state is malformed")
            parsed_scopes[scope_id.strip()] = dict(scope_raw)
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "lease": dict(lease) if isinstance(lease, dict) else None,
            "scopes": parsed_scopes,
        }

    def _scope_locked(self, document: dict[str, Any], scope_id: str) -> dict[str, Any]:
        scopes = document.setdefault("scopes", {})
        key = str(scope_id or "").strip()
        scope = scopes.get(key)
        if not isinstance(scope, dict):
            scope = {
                "last_invoked_at": None,
                "validation_failure_streak": 0,
                "paused_until": None,
                "explicit_bypass_batches": [],
                "pending_triggers": [],
                "pending_repairs": [],
            }
            scopes[key] = scope
        return scope

    def _write_locked(self, document: dict[str, Any]) -> None:
        text = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        self._writer(self._path, text)


class CoreMaintainerScheduler:
    def __init__(
        self,
        queue: CoreCandidateQueue,
        state_store: CoreMaintainerStateStore,
        settings: CoreMaintainerSettings,
        clock: Callable[[], datetime],
    ) -> None:
        self.queue = queue
        self.state = state_store
        self.settings = settings.normalized()
        self.clock = clock

    def evaluate(self, scope_id: str, trigger: MaintainerTrigger | None = None) -> ScheduleDecision:
        settings = self.settings
        if not settings.enabled:
            return ScheduleDecision(False, "skipped_disabled")
        eligible = self.queue.eligible_for(scope_id)
        if trigger is not None:
            self.state.add_pending_trigger(scope_id, trigger)
        now = self.clock()
        if self.state.is_paused(scope_id, now):
            return ScheduleDecision(False, "skipped_paused")
        holder = f"{scope_id}:{uuid.uuid4().hex}"
        ttl = timedelta(minutes=settings.lease_ttl_minutes)
        if not self.state.try_acquire_lease(holder, ttl=ttl):
            return ScheduleDecision(False, "skipped_busy")
        try:
            pending = self.state.pending_triggers(scope_id)
            admitted, reason, bypass = self._admit(scope_id, eligible, pending, now)
            if not admitted:
                self.state.release_lease(holder)
                return ScheduleDecision(False, reason)
            selected = self.select_candidates(
                eligible,
                trigger=_selection_trigger(trigger, pending),
                pending=pending,
                now=now,
            )
            self._consume_selected_triggers(scope_id, pending, selected)
            self.state.mark_invoked(scope_id)
            return ScheduleDecision(
                True,
                "admitted",
                selected=selected,
                lease_holder=holder,
                used_explicit_bypass=bypass,
            )
        except Exception:
            self.state.release_lease(holder)
            raise

    def release(self, lease_holder: str) -> None:
        if lease_holder:
            self.state.release_lease(lease_holder)

    def select_candidates(
        self,
        eligible: Sequence[CoreCandidate],
        *,
        trigger: MaintainerTrigger | None,
        pending: Sequence[MaintainerTrigger],
        now: datetime,
    ) -> tuple[CoreCandidate, ...]:
        by_id = {item.id: item for item in eligible}
        triggering_id = str(trigger.candidate_id or "").strip() if trigger else ""
        pending_ids = {
            str(item.candidate_id).strip()
            for item in pending
            if str(item.candidate_id or "").strip()
        }
        buckets: list[list[CoreCandidate]] = [[], [], [], [], []]
        seen: set[str] = set()

        def _take(candidate: CoreCandidate, bucket: int) -> None:
            if candidate.id in seen:
                return
            seen.add(candidate.id)
            buckets[bucket].append(candidate)

        if triggering_id in by_id and by_id[triggering_id].kind == "explicit":
            _take(by_id[triggering_id], 0)
        for candidate in eligible:
            if candidate.kind == "explicit":
                _take(candidate, 1)
        for candidate in eligible:
            if _is_stale(candidate, now=now, settings=self.settings, config=self.queue.config):
                _take(candidate, 2)
        for candidate in eligible:
            if candidate.id in pending_ids:
                _take(candidate, 3)
        for candidate in eligible:
            _take(candidate, 4)

        ordered: list[CoreCandidate] = []
        for bucket in buckets:
            ordered.extend(sorted(bucket, key=lambda item: _selection_sort_key(item, self.queue.config)))
        return tuple(ordered[: self.settings.max_candidates_per_call])

    def _consume_selected_triggers(
        self,
        scope_id: str,
        pending: Sequence[MaintainerTrigger],
        selected: Sequence[CoreCandidate],
    ) -> None:
        selected_ids = {item.id for item in selected}
        selected_batches = {
            str(evidence.batch_id).strip()
            for item in selected
            for evidence in item.evidence
            if str(evidence.batch_id or "").strip()
        }
        consumed: list[MaintainerTrigger] = []
        leftover: list[MaintainerTrigger] = []
        for item in pending:
            candidate_id_value = str(item.candidate_id or "").strip()
            batch_id = str(item.batch_id or "").strip()
            selected_by_id = bool(candidate_id_value) and candidate_id_value in selected_ids
            selected_by_batch = (
                not candidate_id_value and bool(batch_id) and batch_id in selected_batches
            )
            if selected_by_id or selected_by_batch:
                consumed.append(item)
            else:
                leftover.append(item)
        for item in consumed:
            if item.kind == "explicit" and item.batch_id:
                self.state.record_explicit_batch(scope_id, item.batch_id)
        for candidate in selected:
            if candidate.kind != "explicit":
                continue
            for evidence in candidate.evidence:
                batch = str(evidence.batch_id or "").strip()
                if batch:
                    self.state.record_explicit_batch(scope_id, batch)
        self.state.replace_pending_triggers(scope_id, leftover)

    def _admit(
        self,
        scope_id: str,
        eligible: Sequence[CoreCandidate],
        pending: Sequence[MaintainerTrigger],
        now: datetime,
    ) -> tuple[bool, str, bool]:
        if not eligible:
            repairs = self.state.matching_generation_repairs(
                scope_id,
                [item for item in self.queue.candidates_for(scope_id) if item.status == "pending"],
            )
            if not repairs:
                return False, "skipped_no_eligible", False
        stale = [
            item
            for item in eligible
            if _is_stale(item, now=now, settings=self.settings, config=self.queue.config)
        ]
        three = len(eligible) >= 3
        has_repairs = bool(
            self.state.matching_generation_repairs(
                scope_id,
                [item for item in self.queue.candidates_for(scope_id) if item.status == "pending"],
            )
        )
        if not pending and not three and not stale and not has_repairs:
            return False, "skipped_no_eligible", False
        last_invoked = self.state.last_invoked_at(scope_id)
        in_cooldown = last_invoked is not None and (now - last_invoked) < timedelta(
            hours=self.settings.normal_cooldown_hours
        )
        bypass_batch = ""
        for item in pending:
            if (
                item.kind == "explicit"
                and item.batch_id
                and not self.state.explicit_batch_used(scope_id, item.batch_id)
            ):
                bypass_batch = item.batch_id
                break
        if in_cooldown:
            if bypass_batch:
                return True, "admitted", True
            if has_repairs:
                return True, "admitted", False
            return False, "skipped_cooldown", False
        return True, "admitted", False


class CoreProfileMaintainer:
    def __init__(
        self,
        *,
        api_client: Any,
        memory_store: Any,
        queue: CoreCandidateQueue,
        state_store: CoreMaintainerStateStore,
        settings: CoreMaintainerSettings | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.api_client = api_client
        self.memory_store = memory_store
        self.queue = queue
        self.state_store = state_store
        self.settings = (settings or CoreMaintainerSettings()).normalized()
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.scheduler = CoreMaintainerScheduler(queue, state_store, self.settings, self.clock)

    def run_once(self, scope_id: str, trigger: MaintainerTrigger | None = None) -> MaintainerRunResult:
        settings = self.settings
        metrics = _empty_metrics()
        if not settings.enabled:
            return MaintainerRunResult("skipped_disabled", metrics)
        decision = self.scheduler.evaluate(scope_id, trigger)
        try:
            if not decision.admitted:
                return MaintainerRunResult(decision.reason, metrics)
            repaired, profile = self._repair_partial(scope_id)
            repaired_set = set(repaired)
            selected = tuple(item for item in decision.selected if item.id not in repaired_set)
            if not selected:
                metrics["candidate_count"] = len(repaired)
                metrics["candidate_ids"] = list(repaired)
                return MaintainerRunResult("recovered", metrics)
            metrics["candidate_count"] = len(selected)
            metrics["candidate_ids"] = [item.id for item in selected]
            if profile is None:
                profile = self.memory_store.core_profile()
            system_prompt, user_prompt = _build_prompts(profile, selected)
            try:
                raw = self.api_client.complete_raw(
                    system_prompt,
                    [{"role": "user", "content": user_prompt}],
                    temperature=0.2,
                    response_format={"type": "json_object"},
                    max_tokens=2000,
                    task="background",
                    thinking={"type": "disabled"},
                )
            except Exception:
                return MaintainerRunResult("api_error", metrics)
            try:
                proposal = parse_maintainer_response(raw)
                operations = _validate_proposal(
                    proposal,
                    profile=profile,
                    selected=selected,
                    settings=settings,
                    now=self.clock(),
                    config=self.queue.config,
                )
            except MaintainerParseError:
                self._reject(scope_id, metrics, "parse")
                return MaintainerRunResult("rejected", metrics)
            except MaintainerValidationError as exc:
                self._reject(scope_id, metrics, exc.category)
                return MaintainerRunResult("rejected", metrics)
            migrate_ops = [item for item in operations if item.op == "migrate_legacy"]
            ordinary_ops = [item for item in operations if item.op not in {"keep", "migrate_legacy"}]
            counted = operations
            if migrate_ops and ordinary_ops:
                metrics["ordinary_deferred"] = True
                counted = tuple(item for item in operations if item.op in {"keep", "migrate_legacy"})
            _count_ops(metrics, counted)
            try:
                applied_ids, reviewed_ids, patched = self._apply(proposal.base_updated_at, operations)
            except CoreProfileStorageError:
                return MaintainerRunResult("storage_error", metrics)
            if patched is not None and applied_ids:
                revision = _profile_updated_at(patched)
                by_id = {item.id: item for item in selected}
                self.state_store.record_pending_repairs(
                    scope_id,
                    [
                        {
                            "candidate_id": candidate_id_value,
                            "evidence_fingerprint": candidate_generation_fingerprint(
                                by_id[candidate_id_value]
                            ),
                            "core_revision": revision,
                        }
                        for candidate_id_value in applied_ids
                        if candidate_id_value in by_id
                    ],
                )
            try:
                if applied_ids or reviewed_ids:
                    self.queue.mark_processed(
                        scope_id,
                        applied_ids=applied_ids,
                        reviewed_ids=reviewed_ids,
                    )
            except Exception:
                return MaintainerRunResult("partial_commit", metrics)
            if applied_ids:
                self.state_store.clear_pending_repairs(scope_id, applied_ids)
            self.state_store.reset_validation_streak(scope_id)
            all_keep = bool(counted) and all(item.op == "keep" for item in counted) and not migrate_ops
            return MaintainerRunResult("keep" if all_keep else "applied", metrics)
        finally:
            self.scheduler.release(decision.lease_holder)

    def _repair_partial(self, scope_id: str) -> tuple[tuple[str, ...], Any]:
        profile = self.memory_store.core_profile()
        records = self.state_store.pending_repairs(scope_id)
        if not records:
            return (), profile
        revision = _profile_updated_at(profile if isinstance(profile, Mapping) else None)
        pending = {
            item.id: item
            for item in self.queue.candidates_for(scope_id)
            if item.status == "pending"
        }
        repair_ids: list[str] = []
        stale_ids: list[str] = []
        for record in records:
            candidate_id_value = record["candidate_id"]
            candidate = pending.get(candidate_id_value)
            if candidate is None:
                stale_ids.append(candidate_id_value)
                continue
            if candidate_generation_fingerprint(candidate) != record["evidence_fingerprint"]:
                stale_ids.append(candidate_id_value)
                continue
            if record["core_revision"] != revision:
                stale_ids.append(candidate_id_value)
                continue
            repair_ids.append(candidate_id_value)
        if stale_ids:
            self.state_store.clear_pending_repairs(scope_id, stale_ids)
        if repair_ids:
            self.queue.mark_processed(scope_id, applied_ids=repair_ids, reviewed_ids=())
            self.state_store.clear_pending_repairs(scope_id, repair_ids)
        return tuple(repair_ids), profile

    def _reject(self, scope_id: str, metrics: dict[str, Any], category: str) -> None:
        metrics["validation_rejected"] = category
        self.state_store.record_validation_failure(
            scope_id,
            threshold=self.settings.pause_after_validation_failures,
            pause_hours=self.settings.pause_hours,
        )

    def _apply(
        self,
        base_updated_at: str,
        operations: tuple[MaintainerOperation, ...],
    ) -> tuple[list[str], list[str], Any]:
        applied: list[str] = []
        reviewed: list[str] = []
        for item in operations:
            if item.op == "keep":
                reviewed.extend(item.candidate_ids)
            else:
                applied.extend(item.candidate_ids)
        applied_ids = _unique(applied)
        reviewed_ids = [item for item in _unique(reviewed) if item not in set(applied_ids)]
        migrate_ops = [item for item in operations if item.op == "migrate_legacy"]
        ordinary_ops = [item for item in operations if item.op not in {"keep", "migrate_legacy"}]
        deferred_ids: set[str] = set()
        if migrate_ops and ordinary_ops:
            deferred_ids = {cid for item in ordinary_ops for cid in item.candidate_ids}
            ordinary_ops = []
        applied_ids = [item for item in applied_ids if item not in deferred_ids]
        reviewed_ids = [item for item in reviewed_ids if item not in deferred_ids]
        if not migrate_ops and not ordinary_ops:
            return applied_ids, reviewed_ids, None
        candidate_ids = applied_ids
        patched: Any = None
        if migrate_ops:
            sections = {name: "" for name in CORE_PROFILE_FORMAL_SECTIONS}
            for item in migrate_ops:
                for name, body in item.sections:
                    sections[name] = body
            patched = self.memory_store.patch_core_profile_sections(
                base_updated_at,
                sections,
                candidate_ids=candidate_ids,
                migrate_legacy=True,
            )
        elif ordinary_ops:
            sections = {item.section: item.content for item in ordinary_ops}
            patched = self.memory_store.patch_core_profile_sections(
                base_updated_at,
                sections,
                candidate_ids=candidate_ids,
                migrate_legacy=False,
            )
        return applied_ids, reviewed_ids, patched


def _parse_operation(raw: object) -> MaintainerOperation:
    if not isinstance(raw, dict):
        raise MaintainerParseError("invalid operation keys")
    op = raw.get("op")
    if op not in ALLOWED_OPS or not isinstance(op, str):
        raise MaintainerParseError("invalid op")
    expected = (
        KEEP_OPERATION_KEYS
        if op == "keep"
        else MIGRATE_OPERATION_KEYS
        if op == "migrate_legacy"
        else MUTATING_OPERATION_KEYS
    )
    if set(raw.keys()) != expected:
        raise MaintainerParseError("invalid operation keys")
    reason = raw.get("reason")
    if not isinstance(reason, str):
        raise MaintainerParseError("invalid reason")
    candidate_ids = _parse_id_list(raw.get("candidate_ids"))
    evidence_ids = _parse_id_list(raw.get("evidence_ids"))
    if op == "keep":
        section = _parse_section(raw.get("section"))
        return MaintainerOperation(
            op=op,
            section=section,
            content="",
            reason=reason,
            candidate_ids=candidate_ids,
            evidence_ids=evidence_ids,
        )
    if op == "migrate_legacy":
        return MaintainerOperation(
            op=op,
            section="",
            content="",
            reason=reason,
            candidate_ids=candidate_ids,
            evidence_ids=evidence_ids,
            sections=_parse_sections_map(raw.get("sections")),
        )
    section = _parse_section(raw.get("section"))
    content = raw.get("content")
    if not isinstance(content, str):
        raise MaintainerParseError("invalid content")
    if op in {"refine", "replace"} and content == "":
        raise MaintainerParseError("invalid content")
    if op == "remove" and content != "":
        raise MaintainerParseError("invalid content")
    return MaintainerOperation(
        op=op,
        section=section,
        content=content,
        reason=reason,
        candidate_ids=candidate_ids,
        evidence_ids=evidence_ids,
    )


def _validate_proposal(
    proposal: MaintainerProposal,
    *,
    profile: dict[str, Any] | None,
    selected: Sequence[CoreCandidate],
    settings: CoreMaintainerSettings,
    now: datetime,
    config: CoreCandidateConfig,
) -> tuple[MaintainerOperation, ...]:
    if profile is None:
        raise MaintainerValidationError("base_token")
    if proposal.base_updated_at != _profile_updated_at(profile):
        raise MaintainerValidationError("base_token")
    if not proposal.operations:
        raise MaintainerValidationError("operations")
    selected_by_id = {item.id: item for item in selected}
    current_sections = _profile_sections(profile)
    has_legacy = bool(str(current_sections.get("legacy") or "").strip())
    migrate_ops = [item for item in proposal.operations if item.op == "migrate_legacy"]
    ordinary = [item for item in proposal.operations if item.op not in {"keep", "migrate_legacy"}]
    if len(migrate_ops) > 1:
        raise MaintainerValidationError("limits")
    if len(ordinary) > settings.max_sections_per_call:
        raise MaintainerValidationError("limits")
    if migrate_ops and len(ordinary) > 1:
        raise MaintainerValidationError("limits")
    if has_legacy and ordinary and not migrate_ops:
        raise MaintainerValidationError("limits")
    if ordinary and not has_legacy and migrate_ops:
        raise MaintainerValidationError("limits")
    ordinary_sections = [item.section for item in ordinary]
    if len(ordinary_sections) != len(set(ordinary_sections)):
        raise MaintainerValidationError("duplicate_section")
    keep_ids = {
        candidate_id
        for item in proposal.operations
        if item.op == "keep"
        for candidate_id in item.candidate_ids
    }
    applied_ids = {
        candidate_id
        for item in proposal.operations
        if item.op != "keep"
        for candidate_id in item.candidate_ids
    }
    if keep_ids & applied_ids:
        raise MaintainerValidationError("duplicate_candidate")

    converted: list[MaintainerOperation] = []
    for item in proposal.operations:
        converted.append(
            _validate_operation(
                item,
                selected_by_id=selected_by_id,
                current_sections=current_sections,
                now=now,
                config=config,
            )
        )
    working_sections = dict(current_sections)
    if migrate_ops:
        working_sections = {name: "" for name in CORE_PROFILE_FORMAL_SECTIONS}
        for item in converted:
            if item.op == "migrate_legacy":
                for name, body in item.sections:
                    working_sections[name] = body
        working_sections.pop("legacy", None)
    effective = []
    for item in converted:
        if item.op in {"refine", "replace"}:
            current = str(working_sections.get(item.section) or "")
            if _normalize_text(item.content) == _normalize_text(current):
                item = MaintainerOperation(
                    op="keep",
                    section=item.section,
                    content=item.content,
                    reason=item.reason,
                    candidate_ids=item.candidate_ids,
                    evidence_ids=item.evidence_ids,
                    sections=item.sections,
                )
        if item.op == "migrate_legacy":
            for name, body in item.sections:
                working_sections[name] = body
        elif item.op != "keep":
            working_sections[item.section] = item.content
        effective.append(item)

    non_keep = [item for item in effective if item.op != "keep"]
    old_body = _formal_body(current_sections)
    new_sections = dict(current_sections)
    if any(item.op == "migrate_legacy" for item in effective):
        new_sections = {name: "" for name in CORE_PROFILE_FORMAL_SECTIONS}
        new_sections.pop("legacy", None)
        for item in effective:
            if item.op == "migrate_legacy":
                for name, body in item.sections:
                    new_sections[name] = body
    for item in effective:
        if item.op in {"refine", "replace", "remove"}:
            new_sections[item.section] = item.content
    new_body = _formal_body(new_sections)
    for item in effective:
        if item.op in {"refine", "replace"}:
            support = _grounding_support(
                str(current_sections.get(item.section) or ""),
                [selected_by_id[cid] for cid in item.candidate_ids if cid in selected_by_id],
                item.evidence_ids,
            )
            if _unsupported_protected_tokens(support, item.content):
                raise MaintainerValidationError("grounding")
    _assert_section_anchors(current_sections, new_sections, effective, selected_by_id)
    has_remove = any(item.op == "remove" for item in effective)
    if non_keep and not has_remove and old_body and len(new_body) < len(old_body) * 0.6:
        raise MaintainerValidationError("shrink")
    return tuple(effective)


def _validate_operation(
    item: MaintainerOperation,
    *,
    selected_by_id: Mapping[str, CoreCandidate],
    current_sections: Mapping[str, str],
    now: datetime,
    config: CoreCandidateConfig,
) -> MaintainerOperation:
    referenced: list[CoreCandidate] = []
    for candidate_id_value in item.candidate_ids:
        candidate = selected_by_id.get(candidate_id_value)
        if candidate is None or candidate.status != "pending":
            raise MaintainerValidationError("references")
        if not is_eligible(candidate, now=now, config=config):
            raise MaintainerValidationError("references")
        if item.op != "migrate_legacy" and candidate.target_section != item.section:
            raise MaintainerValidationError("section_binding")
        referenced.append(candidate)
    allowed_evidence = {evidence.id for candidate in referenced for evidence in candidate.evidence}
    if any(evidence_id not in allowed_evidence for evidence_id in item.evidence_ids):
        raise MaintainerValidationError("evidence_binding")
    if item.op == "migrate_legacy":
        for _name, body in item.sections:
            if _has_forbidden_language(body):
                raise MaintainerValidationError("language")
    elif item.op != "keep" and _has_forbidden_language(item.content):
        raise MaintainerValidationError("language")
    if item.op == "remove" and not any(_is_correction_candidate(candidate) for candidate in referenced):
        raise MaintainerValidationError("remove_requires_correction")
    _ = current_sections
    return item


def _build_prompts(
    profile: dict[str, Any] | None,
    selected: Sequence[CoreCandidate],
) -> tuple[str, str]:
    system_prompt = (
        "あなたは Sakura です。第一人称で、自分と相手の安定した関係認識だけを保守します。"
        "你是 Sakura，只用第一人称维护自己的稳定认识。"
        "出力は指定キーだけの JSON。履歴・気分・カード・親密ガイドは見ない。"
    )
    sections = _profile_sections(profile)
    section_lines = []
    for name in CORE_PROFILE_FORMAL_SECTIONS:
        body = str(sections.get(name) or "").strip()
        if body:
            section_lines.append(f"＜{name}＞\n{body}")
    if str(sections.get("legacy") or "").strip():
        section_lines.append(f"＜legacy＞\n{sections['legacy']}")
    candidate_blocks = []
    for item in selected:
        evidence_lines = []
        for evidence in item.evidence:
            evidence_lines.append(
                f"- {evidence.id}: user={evidence.user_excerpt} / assistant={evidence.assistant_excerpt}"
            )
        candidate_blocks.append(
            "\n".join(
                [
                    f"id={item.id}",
                    f"kind={item.kind}",
                    f"section={item.target_section}",
                    f"subject={item.subject_key}",
                    f"claim={item.claim}",
                    "evidence:",
                    *evidence_lines,
                ]
            )
        )
    user_prompt = "【現在の常駐档案】\n" + ("\n\n".join(section_lines) or "(empty)")
    user_prompt += "\n\n【候補】\n" + ("\n\n".join(candidate_blocks) or "(none)")
    user_prompt += (
        "\n\nprotocol: keep/refine/replace/remove/migrate_legacy。"
        "keep omits content; migrate_legacy uses sections map。"
        "keys: base_updated_at, operations[].op|section|content|reason|candidate_ids|evidence_ids|sections。"
        f"base_updated_at={_profile_updated_at(profile)}"
    )
    return system_prompt, user_prompt


def _profile_updated_at(profile: Mapping[str, Any] | None) -> str:
    if not isinstance(profile, Mapping):
        return ""
    metadata = profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {}
    return str(metadata.get("updated_at") or profile.get("updated_at") or "").strip()


def _profile_sections(profile: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(profile, Mapping):
        return {}
    raw = profile.get("sections")
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value or "") for key, value in raw.items()}


def _formal_body(sections: Mapping[str, str]) -> str:
    parts = [str(sections.get(name) or "") for name in CORE_PROFILE_FORMAL_SECTIONS]
    return "".join(parts)


def _normalize_text(text: str) -> str:
    return " ".join(str(text).split())


def _protected_tokens(text: str) -> Counter[str]:
    normalized = _normalize_text(text)
    tokens = Counter(_QUOTED_TOKEN.findall(normalized))
    tokens.update(_NUMBER_TOKEN.findall(normalized))
    return tokens


def _section_anchor_tokens(section_name: str, text: str) -> Counter[str]:
    normalized = _normalize_text(text)
    tokens = _protected_tokens(normalized)
    for name in _ADDRESS_NAME.findall(normalized):
        captured = str(name or "").strip()
        if captured:
            tokens[captured] += 1
    for term in _IDENTITY_TERMS:
        count = normalized.count(term)
        if count:
            tokens[term] += count
    if section_name == "大切な約束と境界":
        for run in _CJK_RUN.findall(normalized):
            if run not in _AGREEMENT_STOP:
                tokens[run] += 1
    return tokens


def _missing_section_anchors(section_name: str, old_text: str, new_text: str) -> bool:
    old_tokens = _section_anchor_tokens(section_name, old_text)
    new_tokens = _section_anchor_tokens(section_name, new_text)
    return any(new_tokens[token] < count for token, count in old_tokens.items())


def _operation_changes_section(item: MaintainerOperation, section_name: str) -> bool:
    if item.op == "keep":
        return False
    if item.op == "migrate_legacy":
        return any(name == section_name for name, _body in item.sections)
    return item.section == section_name


def _operation_has_bound_correction(
    item: MaintainerOperation,
    section_name: str,
    selected_by_id: Mapping[str, CoreCandidate],
) -> bool:
    for candidate_id_value in item.candidate_ids:
        candidate = selected_by_id.get(candidate_id_value)
        if (
            candidate is not None
            and _is_correction_candidate(candidate)
            and candidate.target_section == section_name
        ):
            return True
    return False


def _assert_section_anchors(
    old_sections: Mapping[str, str],
    new_sections: Mapping[str, str],
    operations: Sequence[MaintainerOperation],
    selected_by_id: Mapping[str, CoreCandidate],
) -> None:
    for name in CORE_PROFILE_FORMAL_SECTIONS:
        old_text = str(old_sections.get(name) or "")
        new_text = str(new_sections.get(name) or "")
        if _normalize_text(old_text) == _normalize_text(new_text):
            continue
        bound = any(
            _operation_changes_section(item, name)
            and _operation_has_bound_correction(item, name, selected_by_id)
            for item in operations
        )
        if bound:
            continue
        if _missing_section_anchors(name, old_text, new_text):
            raise MaintainerValidationError("anchors")


def _grounding_support(
    old_section: str,
    candidates: Sequence[CoreCandidate],
    evidence_ids: Sequence[str],
) -> str:
    parts = [old_section]
    allowed = set(evidence_ids)
    for candidate in candidates:
        parts.append(candidate.claim)
        for evidence in candidate.evidence:
            if evidence.id in allowed:
                parts.append(evidence.user_excerpt)
                parts.append(evidence.assistant_excerpt)
    return "\n".join(parts)


def _unsupported_protected_tokens(support: str, new_text: str) -> bool:
    support_tokens = _protected_tokens(support)
    new_tokens = _protected_tokens(new_text)
    return any(new_tokens[token] > support_tokens[token] for token in new_tokens)


def _has_forbidden_language(content: str) -> bool:
    lowered = content.lower()
    return any(token.lower() in lowered for token in FORBIDDEN_LANGUAGE)


def _is_correction_candidate(candidate: CoreCandidate) -> bool:
    return candidate.kind == "explicit" and (
        candidate.subject_key == "relationship.correction"
        or candidate.subject_key.startswith("relationship.correction.")
    )


def _is_stale(
    candidate: CoreCandidate,
    *,
    now: datetime,
    settings: CoreMaintainerSettings,
    config: CoreCandidateConfig,
) -> bool:
    stamp = eligible_since(candidate, config=config)
    parsed = parse_iso_datetime(stamp or "")
    if parsed is None:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return current - parsed >= timedelta(hours=settings.stale_eligible_hours)


def _selection_sort_key(
    candidate: CoreCandidate,
    config: CoreCandidateConfig,
) -> tuple[datetime, str]:
    stamp = parse_iso_datetime(eligible_since(candidate, config=config) or "")
    if stamp is None:
        stamp = datetime.min.replace(tzinfo=timezone.utc)
    elif stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (stamp, candidate.id)


def _selection_trigger(
    trigger: MaintainerTrigger | None,
    pending: Sequence[MaintainerTrigger],
) -> MaintainerTrigger | None:
    if trigger is not None:
        return trigger
    for item in pending:
        if item.kind == "explicit" and item.candidate_id:
            return item
    return pending[0] if pending else None


def _trigger_from_raw(raw: object) -> MaintainerTrigger | None:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "").strip()
    if not kind:
        return None
    return MaintainerTrigger(
        kind=kind,
        batch_id=str(raw.get("batch_id") or "").strip(),
        candidate_id=str(raw.get("candidate_id") or "").strip(),
    )


def _parse_id_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise MaintainerParseError("invalid ids")
    parsed: list[str] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, str) or not item.strip():
            raise MaintainerParseError("invalid ids")
        if item in seen:
            raise MaintainerParseError("duplicate ids")
        seen.add(item)
        parsed.append(item)
    if not parsed:
        raise MaintainerParseError("invalid ids")
    return tuple(parsed)


def _parse_section(value: object) -> str:
    if not isinstance(value, str) or value not in _FORMAL_SECTION_SET:
        raise MaintainerParseError("invalid section")
    return value


def _parse_sections_map(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict) or not value:
        raise MaintainerParseError("invalid sections")
    pairs: list[tuple[str, str]] = []
    for key, body in value.items():
        if not isinstance(key, str) or key not in _FORMAL_SECTION_SET:
            raise MaintainerParseError("invalid sections")
        if isinstance(body, bool) or not isinstance(body, str):
            raise MaintainerParseError("invalid sections")
        pairs.append((key, body))
    return tuple(pairs)


def _repair_record_from_raw(raw: object) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    candidate_id_value = str(raw.get("candidate_id") or "").strip()
    fingerprint = str(raw.get("evidence_fingerprint") or "").strip()
    revision = str(raw.get("core_revision") or "").strip()
    if not candidate_id_value or not fingerprint or not revision:
        return None
    return {
        "candidate_id": candidate_id_value,
        "evidence_fingerprint": fingerprint,
        "core_revision": revision,
    }


def _lease_is_active(lease: Mapping[str, Any], now: datetime, ttl: timedelta) -> bool:
    holder = str(lease.get("holder") or "").strip()
    if not holder:
        return False
    acquired = parse_iso_datetime(str(lease.get("acquired_at") or ""))
    if acquired is None:
        return False
    if acquired.tzinfo is None:
        acquired = acquired.replace(tzinfo=timezone.utc)
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return current - acquired < ttl


def _unique(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in values:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _empty_metrics() -> dict[str, Any]:
    return {
        "candidate_count": 0,
        "candidate_ids": [],
        "ops": {"keep": 0, "refine": 0, "replace": 0, "remove": 0, "migrate_legacy": 0},
        "validation_rejected": None,
        "input_tokens": None,
        "output_tokens": None,
    }


def _count_ops(metrics: dict[str, Any], operations: Sequence[MaintainerOperation]) -> None:
    counts = metrics["ops"]
    for item in operations:
        if item.op in counts:
            counts[item.op] += 1


def _clamp_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _clamp_float(value: Any, minimum: float, maximum: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    if number != number:  # NaN
        number = default
    return max(minimum, min(maximum, number))


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default
