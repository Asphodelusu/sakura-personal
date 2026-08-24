"""常驻档案候选队列：确定性合并、eligibility 与原子持久化。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn

from app.agent.time_awareness import parse_iso_datetime
from app.storage.atomic import atomic_write_text

SCHEMA_VERSION = 1
FORMAL_TARGET_SECTIONS = frozenset(
    {
        "今の関係",
        "あなたについて知っていること",
        "今の私",
        "大切な約束と境界",
    }
)
CANDIDATE_KINDS = frozenset({"explicit", "observed"})
CANDIDATE_STATUSES = frozenset({"pending", "applied", "reviewed", "expired"})
PROCESSED_STATUSES = frozenset({"applied", "reviewed", "expired"})
EXPLICIT_SUBJECT_PREFIXES = (
    "relationship.identity",
    "relationship.address",
    "relationship.agreement",
    "relationship.boundary",
    "relationship.correction",
)
CONFIDENCE_QUANTUM = Decimal("0.0001")

_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}


class CoreCandidateQueueError(RuntimeError):
    """候选队列损坏或结构非法；不得覆盖源文件。"""


@dataclass(frozen=True)
class CoreCandidateConfig:
    observed_min_evidence: int = 3
    observed_min_batches: int = 2
    observed_min_span_minutes: int = 30
    observed_min_confidence: float = 0.80
    explicit_min_confidence: float = 0.90
    excerpt_max_chars: int = 160
    claim_max_chars: int = 240
    max_evidence: int = 5
    max_candidates_per_scope: int = 50
    processed_retention_days: int = 7
    pending_expire_days: int = 30


@dataclass(frozen=True)
class CoreEvidence:
    id: str
    user_excerpt: str
    assistant_excerpt: str
    observed_at: str
    batch_id: str
    confidence: float = 0.0
    kind: str = "observed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_excerpt": self.user_excerpt,
            "assistant_excerpt": self.assistant_excerpt,
            "observed_at": self.observed_at,
            "batch_id": self.batch_id,
            "confidence": self.confidence,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class CoreCandidate:
    id: str
    kind: str
    target_section: str
    subject_key: str
    claim: str
    evidence: tuple[CoreEvidence, ...]
    confidence: float
    first_seen_at: str
    last_seen_at: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "target_section": self.target_section,
            "subject_key": self.subject_key,
            "claim": self.claim,
            "evidence": [item.to_dict() for item in self.evidence],
            "confidence": self.confidence,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "status": self.status,
        }


def _clip(value: str, max_chars: int) -> str:
    return "".join(list(str(value or ""))[: max(0, max_chars)])


def _clean_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def _normalize_for_hash(value: str) -> str:
    return " ".join(_clean_text(value).split())


def _decimal_confidence(value: float) -> Decimal:
    return Decimal(str(value)).quantize(CONFIDENCE_QUANTUM, rounding=ROUND_HALF_UP)


def _meets_threshold(value: float, threshold: float) -> bool:
    return _decimal_confidence(value) >= _decimal_confidence(threshold)


def _as_confidence(value: object, *, default: float | None = None) -> float:
    if value is None and default is not None:
        return _quantize_confidence(default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid confidence")
    number = float(value)
    if not math.isfinite(number) or number < 0.0 or number > 1.0:
        raise ValueError("invalid confidence")
    return _quantize_confidence(number)


def _quantize_confidence(value: float) -> float:
    return float(_decimal_confidence(value))


def _parse_dt(value: str | None) -> datetime | None:
    return parse_iso_datetime(value)


def _minute_bucket(observed_at: str) -> str:
    parsed = _parse_dt(observed_at)
    if parsed is None:
        raise ValueError("invalid observed_at")
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}{digest[:16]}"


def candidate_id(target_section: str, subject_key: str) -> str:
    return _stable_id("cc_", target_section, subject_key)


def evidence_id(
    *,
    user_excerpt: str,
    assistant_excerpt: str,
    observed_at: str,
    batch_id: str,
) -> str:
    return _stable_id(
        "ce_",
        _normalize_for_hash(user_excerpt),
        _normalize_for_hash(assistant_excerpt),
        _minute_bucket(observed_at),
        batch_id.strip(),
    )


def is_supported_explicit_subject(subject_key: str) -> bool:
    key = str(subject_key or "").strip()
    return any(key == prefix or key.startswith(f"{prefix}.") for prefix in EXPLICIT_SUBJECT_PREFIXES)


def _mean_confidence(evidence: tuple[CoreEvidence, ...]) -> float:
    if not evidence:
        return 0.0
    total = sum(_decimal_confidence(item.confidence) for item in evidence)
    mean = (total / len(evidence)).quantize(CONFIDENCE_QUANTUM, rounding=ROUND_HALF_UP)
    return float(mean)


def _has_bilateral_evidence(evidence: tuple[CoreEvidence, ...]) -> bool:
    return any(
        _clean_text(item.user_excerpt) and _clean_text(item.assistant_excerpt)
        for item in evidence
    )


def _evidence_span(candidate: CoreCandidate) -> timedelta:
    stamps = [_parse_dt(item.observed_at) for item in candidate.evidence]
    present = [item for item in stamps if item is not None]
    if len(present) < 2:
        return timedelta(0)
    return max(present) - min(present)


def _kind_from_evidence(evidence: tuple[CoreEvidence, ...], fallback: str) -> str:
    if any(item.kind == "explicit" for item in evidence):
        return "explicit"
    if any(item.kind == "observed" for item in evidence):
        return "observed"
    return fallback if fallback in CANDIDATE_KINDS else "observed"


def is_eligible(
    candidate: CoreCandidate,
    *,
    now: datetime,
    config: CoreCandidateConfig | None = None,
) -> bool:
    rules = config or CoreCandidateConfig()
    if candidate.status != "pending":
        return False
    first_seen = _parse_dt(candidate.first_seen_at)
    if first_seen is not None and now - first_seen >= timedelta(days=rules.pending_expire_days):
        return False
    if candidate.kind == "explicit":
        explicit_evidence = tuple(item for item in candidate.evidence if item.kind == "explicit")
        if not explicit_evidence:
            return False
        best = max(item.confidence for item in explicit_evidence)
        return (
            is_supported_explicit_subject(candidate.subject_key)
            and _meets_threshold(best, rules.explicit_min_confidence)
            and _has_bilateral_evidence(explicit_evidence)
        )
    if candidate.kind == "observed":
        batches = {item.batch_id for item in candidate.evidence}
        return (
            len(candidate.evidence) >= rules.observed_min_evidence
            and len(batches) >= rules.observed_min_batches
            and _evidence_span(candidate) >= timedelta(minutes=rules.observed_min_span_minutes)
            and _meets_threshold(candidate.confidence, rules.observed_min_confidence)
        )
    return False


def _default_clock() -> datetime:
    return datetime.now().astimezone()


def _default_writer(path: Path, text: str) -> None:
    atomic_write_text(path, text)


def _lock_key(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _thread_lock_for(path: Path) -> threading.Lock:
    key = _lock_key(path)
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


def _lock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _exclusive_path_lock(path: Path) -> Iterator[None]:
    thread_lock = _thread_lock_for(path)
    thread_lock.acquire()
    handle: Any | None = None
    locked = False
    try:
        lock_path = path.with_name(path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        _lock_file(handle)
        locked = True
        yield
    finally:
        if handle is not None:
            if locked:
                try:
                    _unlock_file(handle)
                except OSError:
                    pass
            handle.close()
        thread_lock.release()


def _copy_scopes(scopes: dict[str, list[CoreCandidate]]) -> dict[str, list[CoreCandidate]]:
    return {scope_id: list(items) for scope_id, items in scopes.items()}


def _scopes_fingerprint(scopes: dict[str, list[CoreCandidate]]) -> tuple[Any, ...]:
    return tuple((scope_id, tuple(items)) for scope_id, items in sorted(scopes.items()))


class CoreCandidateQueue:
    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        config: CoreCandidateConfig | None = None,
        writer: Callable[[Path, str], None] | None = None,
    ) -> None:
        self._path = Path(path)
        self._clock = clock or _default_clock
        self.config = config or CoreCandidateConfig()
        self._writer = writer or _default_writer
        self._scopes: dict[str, list[CoreCandidate]] = {}

    def ingest(self, scope_id: str, payload: Mapping[str, Any]) -> CoreCandidate:
        with _exclusive_path_lock(self._path):
            loaded = self._load_from_disk()
            working = self._housekeep_scopes(_copy_scopes(loaded))
            scope = str(scope_id or "").strip()
            if not scope:
                raise ValueError("scope_id is required")
            incoming = self._parse_ingest_payload(payload)
            items = list(working.get(scope, []))
            working[scope] = self._prune_scope(self._merge_or_append(items, incoming))
            self._commit(working)
            return self._find(working, scope, incoming.id)

    def candidates_for(self, scope_id: str) -> tuple[CoreCandidate, ...]:
        with _exclusive_path_lock(self._path):
            return self._candidates_for_locked(scope_id)

    def eligible_for(self, scope_id: str) -> tuple[CoreCandidate, ...]:
        with _exclusive_path_lock(self._path):
            now = self._clock()
            return tuple(
                item
                for item in self._candidates_for_locked(scope_id)
                if is_eligible(item, now=now, config=self.config)
            )

    def _candidates_for_locked(self, scope_id: str) -> tuple[CoreCandidate, ...]:
        loaded = self._load_from_disk()
        working = self._housekeep_scopes(_copy_scopes(loaded))
        if _scopes_fingerprint(working) != _scopes_fingerprint(loaded):
            self._commit(working)
        else:
            self._scopes = working
        return tuple(self._scopes.get(str(scope_id or "").strip(), ()))

    def _now_iso(self) -> str:
        return self._clock().isoformat()

    def _load_from_disk(self) -> dict[str, list[CoreCandidate]]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            self._fail_load("core review queue is malformed", exc)
        except OSError as exc:
            self._fail_load("core review queue could not be read", exc)
        try:
            return self._parse_document(raw)
        except CoreCandidateQueueError:
            raise
        except (TypeError, ValueError) as exc:
            self._fail_load("core review queue is malformed", exc)

    def _fail_load(self, message: str, cause: BaseException) -> NoReturn:
        raise CoreCandidateQueueError(message) from cause

    def _parse_document(self, raw: object) -> dict[str, list[CoreCandidate]]:
        if not isinstance(raw, dict):
            raise CoreCandidateQueueError("core review queue is malformed")
        version = raw.get("schema_version", SCHEMA_VERSION)
        if isinstance(version, bool) or version != SCHEMA_VERSION:
            raise CoreCandidateQueueError("core review queue is malformed")
        scopes_raw = raw.get("scopes", {})
        if not isinstance(scopes_raw, dict):
            raise CoreCandidateQueueError("core review queue is malformed")
        parsed: dict[str, list[CoreCandidate]] = {}
        for scope_id, scope_raw in scopes_raw.items():
            if not isinstance(scope_id, str) or not scope_id.strip():
                raise CoreCandidateQueueError("core review queue is malformed")
            if not isinstance(scope_raw, dict):
                raise CoreCandidateQueueError("core review queue is malformed")
            candidates_raw = scope_raw.get("candidates", [])
            if not isinstance(candidates_raw, list):
                raise CoreCandidateQueueError("core review queue is malformed")
            candidates = [self._parse_stored_candidate(item) for item in candidates_raw]
            seen_ids: set[str] = set()
            seen_keys: set[tuple[str, str]] = set()
            for item in candidates:
                merge_key = (item.target_section, item.subject_key)
                if item.id in seen_ids or merge_key in seen_keys:
                    raise CoreCandidateQueueError("core review queue is malformed")
                seen_ids.add(item.id)
                seen_keys.add(merge_key)
            parsed[scope_id.strip()] = candidates
        return parsed

    def _parse_stored_candidate(self, raw: object) -> CoreCandidate:
        if not isinstance(raw, dict):
            raise CoreCandidateQueueError("core review queue is malformed")
        try:
            kind = str(raw.get("kind") or "").strip()
            status = str(raw.get("status") or "").strip()
            target_section = str(raw.get("target_section") or "").strip()
            subject_key = str(raw.get("subject_key") or "").strip()
            if kind not in CANDIDATE_KINDS or status not in CANDIDATE_STATUSES:
                raise ValueError("unknown kind or status")
            if target_section not in FORMAL_TARGET_SECTIONS or not subject_key:
                raise ValueError("unknown target section")
            evidence_raw = raw.get("evidence")
            if not isinstance(evidence_raw, list):
                raise ValueError("invalid evidence")
            evidence = tuple(self._parse_stored_evidence(item, fallback_kind=kind) for item in evidence_raw)
            if len(evidence) > self.config.max_evidence:
                raise ValueError("evidence exceeds cap")
            evidence_ids = [item.id for item in evidence]
            if len(evidence_ids) != len(set(evidence_ids)):
                raise ValueError("duplicate evidence")
            first_seen_at = str(raw.get("first_seen_at") or "")
            last_seen_at = str(raw.get("last_seen_at") or "")
            if _parse_dt(first_seen_at) is None or _parse_dt(last_seen_at) is None:
                raise ValueError("invalid timestamps")
            expected_id = candidate_id(target_section, subject_key)
            stored_id = str(raw.get("id") or "").strip()
            if stored_id and stored_id != expected_id:
                raise ValueError("inconsistent candidate id")
            recomputed_confidence = _mean_confidence(evidence)
            if "confidence" in raw and raw.get("confidence") is not None:
                stored_confidence = _as_confidence(raw.get("confidence"))
                if _decimal_confidence(stored_confidence) != _decimal_confidence(recomputed_confidence):
                    raise ValueError("inconsistent confidence")
            recomputed_kind = _kind_from_evidence(evidence, kind)
            if recomputed_kind != kind:
                raise ValueError("inconsistent kind")
            return CoreCandidate(
                id=expected_id,
                kind=recomputed_kind,
                target_section=target_section,
                subject_key=subject_key,
                claim=_clip(_clean_text(raw.get("claim")), self.config.claim_max_chars),
                evidence=evidence,
                confidence=recomputed_confidence,
                first_seen_at=first_seen_at,
                last_seen_at=last_seen_at,
                status=status,
            )
        except (TypeError, ValueError) as exc:
            raise CoreCandidateQueueError("core review queue is malformed") from exc

    def _parse_stored_evidence(self, raw: object, *, fallback_kind: str) -> CoreEvidence:
        if not isinstance(raw, dict):
            raise ValueError("invalid evidence")
        user_excerpt = _clip(_clean_text(raw.get("user_excerpt")), self.config.excerpt_max_chars)
        assistant_excerpt = _clip(_clean_text(raw.get("assistant_excerpt")), self.config.excerpt_max_chars)
        observed_at = str(raw.get("observed_at") or "")
        batch_id = str(raw.get("batch_id") or "").strip()
        if not batch_id or _parse_dt(observed_at) is None:
            raise ValueError("invalid evidence")
        expected_id = evidence_id(
            user_excerpt=user_excerpt,
            assistant_excerpt=assistant_excerpt,
            observed_at=observed_at,
            batch_id=batch_id,
        )
        stored_id = str(raw.get("id") or "").strip()
        if stored_id and stored_id != expected_id:
            raise ValueError("inconsistent evidence id")
        kind = str(raw.get("kind") or "").strip() or fallback_kind
        if kind not in CANDIDATE_KINDS:
            raise ValueError("unknown evidence kind")
        return CoreEvidence(
            id=expected_id,
            user_excerpt=user_excerpt,
            assistant_excerpt=assistant_excerpt,
            observed_at=observed_at,
            batch_id=batch_id,
            confidence=_as_confidence(raw.get("confidence"), default=0.0),
            kind=kind,
        )

    def _parse_ingest_payload(self, payload: Mapping[str, Any]) -> CoreCandidate:
        kind = str(payload.get("kind") or "").strip()
        if kind not in CANDIDATE_KINDS:
            raise ValueError("unknown candidate kind")
        target_section = str(payload.get("target_section") or "").strip()
        if target_section not in FORMAL_TARGET_SECTIONS:
            raise ValueError("unknown target section")
        subject_key = str(payload.get("subject_key") or "").strip()
        if not subject_key:
            raise ValueError("subject_key is required")
        evidence = self._parse_ingest_evidence(payload, kind=kind)
        observed = _parse_dt(evidence.observed_at) or self._clock()
        stamp = observed.isoformat()
        return CoreCandidate(
            id=candidate_id(target_section, subject_key),
            kind=kind,
            target_section=target_section,
            subject_key=subject_key,
            claim=_clip(_clean_text(payload.get("claim")), self.config.claim_max_chars),
            evidence=(evidence,),
            confidence=evidence.confidence,
            first_seen_at=stamp,
            last_seen_at=stamp,
            status="pending",
        )

    def _parse_ingest_evidence(self, payload: Mapping[str, Any], *, kind: str) -> CoreEvidence:
        batch_id = str(payload.get("batch_id") or "").strip()
        if not batch_id:
            raise ValueError("batch_id is required")
        observed_at = str(payload.get("observed_at") or "").strip() or self._now_iso()
        if _parse_dt(observed_at) is None:
            raise ValueError("invalid observed_at")
        user_excerpt = _clip(_clean_text(payload.get("user_excerpt")), self.config.excerpt_max_chars)
        assistant_excerpt = _clip(
            _clean_text(payload.get("assistant_excerpt")),
            self.config.excerpt_max_chars,
        )
        confidence = _as_confidence(payload.get("confidence"), default=0.0)
        return CoreEvidence(
            id=evidence_id(
                user_excerpt=user_excerpt,
                assistant_excerpt=assistant_excerpt,
                observed_at=observed_at,
                batch_id=batch_id,
            ),
            user_excerpt=user_excerpt,
            assistant_excerpt=assistant_excerpt,
            observed_at=observed_at,
            batch_id=batch_id,
            confidence=confidence,
            kind=kind,
        )

    def _merge_or_append(
        self,
        items: list[CoreCandidate],
        incoming: CoreCandidate,
    ) -> list[CoreCandidate]:
        for index, existing in enumerate(items):
            if (
                existing.target_section == incoming.target_section
                and existing.subject_key == incoming.subject_key
            ):
                items[index] = self._merge_candidates(existing, incoming)
                return items
        items.append(incoming)
        return items

    def _merge_candidates(self, existing: CoreCandidate, incoming: CoreCandidate) -> CoreCandidate:
        evidence = list(existing.evidence)
        by_id = {item.id: index for index, item in enumerate(evidence)}
        retained_incoming: set[str] = set()
        added_new = False
        for item in incoming.evidence:
            if item.id in by_id:
                index = by_id[item.id]
                if item.kind == "explicit" and evidence[index].kind != "explicit":
                    evidence[index] = replace(evidence[index], kind="explicit")
                retained_incoming.add(item.id)
                continue
            if len(evidence) >= self.config.max_evidence:
                oldest_index = min(
                    range(len(evidence)),
                    key=lambda idx: (
                        _parse_dt(evidence[idx].observed_at) or datetime.min.replace(tzinfo=timezone.utc),
                        evidence[idx].id,
                    ),
                )
                oldest = evidence[oldest_index]
                incoming_at = _parse_dt(item.observed_at)
                oldest_at = _parse_dt(oldest.observed_at)
                if incoming_at is None or oldest_at is None or incoming_at <= oldest_at:
                    continue
                evidence.pop(oldest_index)
                by_id = {entry.id: idx for idx, entry in enumerate(evidence)}
            evidence.append(item)
            by_id[item.id] = len(evidence) - 1
            retained_incoming.add(item.id)
            added_new = True
        merged_evidence = tuple(evidence)
        first_seen = existing.first_seen_at
        last_seen = existing.last_seen_at
        incoming_last = _parse_dt(incoming.last_seen_at)
        existing_last = _parse_dt(existing.last_seen_at)
        if incoming_last is not None and (existing_last is None or incoming_last > existing_last):
            last_seen = incoming.last_seen_at
        claim = existing.claim
        if retained_incoming and incoming.claim:
            claim = incoming.claim
        kind = _kind_from_evidence(merged_evidence, existing.kind)
        status = existing.status
        if status != "pending" and added_new:
            status = "pending"
        return CoreCandidate(
            id=existing.id,
            kind=kind,
            target_section=existing.target_section,
            subject_key=existing.subject_key,
            claim=claim,
            evidence=merged_evidence,
            confidence=_mean_confidence(merged_evidence),
            first_seen_at=first_seen,
            last_seen_at=last_seen,
            status=status,
        )

    def _find(
        self,
        scopes: dict[str, list[CoreCandidate]],
        scope_id: str,
        candidate_id_value: str,
    ) -> CoreCandidate:
        for item in scopes.get(scope_id, ()):
            if item.id == candidate_id_value:
                return item
        raise CoreCandidateQueueError("candidate was not stored")

    def _housekeep_scopes(
        self, scopes: dict[str, list[CoreCandidate]]
    ) -> dict[str, list[CoreCandidate]]:
        now = self._clock()
        expire_after = timedelta(days=self.config.pending_expire_days)
        retain_after = timedelta(days=self.config.processed_retention_days)
        cleaned: dict[str, list[CoreCandidate]] = {}
        for scope_id, items in scopes.items():
            kept: list[CoreCandidate] = []
            for item in items:
                current = item
                first_seen = _parse_dt(current.first_seen_at)
                if (
                    current.status == "pending"
                    and first_seen is not None
                    and now - first_seen >= expire_after
                ):
                    current = replace(current, status="expired", last_seen_at=now.isoformat())
                if current.status in PROCESSED_STATUSES:
                    marker = _parse_dt(current.last_seen_at)
                    if marker is not None and now - marker >= retain_after:
                        continue
                kept.append(current)
            cleaned[scope_id] = self._prune_scope(kept)
        return cleaned

    def _prune_scope(self, items: list[CoreCandidate]) -> list[CoreCandidate]:
        limit = self.config.max_candidates_per_scope
        if len(items) <= limit:
            return items
        fallback = datetime.min.replace(tzinfo=timezone.utc)

        def sort_key(item: CoreCandidate) -> tuple[datetime, str]:
            return (_parse_dt(item.first_seen_at) or fallback, item.id)

        return sorted(items, key=sort_key)[-limit:]

    def _commit(self, scopes: dict[str, list[CoreCandidate]]) -> None:
        self._write_scopes(scopes)
        self._scopes = scopes

    def _write_scopes(self, scopes: dict[str, list[CoreCandidate]]) -> None:
        if not any(scopes.values()) and not self._path.exists():
            return
        document = {
            "schema_version": SCHEMA_VERSION,
            "scopes": {
                scope_id: {"candidates": [item.to_dict() for item in items]}
                for scope_id, items in scopes.items()
            },
        }
        text = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        self._writer(self._path, text)
