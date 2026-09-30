"""Profile-candidate persistence and one tracked maintenance pass after curation."""
import threading
from pathlib import Path

try:
    from .personal_core_candidates import CoreCandidateQueue, CoreCandidateQueueError, exclusive_json_path_lock
    from .personal_core_maintainer import (
        CoreMaintainerSettings,
        CoreMaintainerStateError,
        CoreMaintainerStateStore,
        CoreProfileMaintainer,
        MaintainerTrigger,
    )
    from .personal_core_profile import (
        load_personal_core_profile_record,
        patch_personal_core_profile_sections,
        upgrade_personal_core_profile,
    )
    from .personal_records import _require_write_mode
    from .support import OperationCancelled, log_event
except ImportError:
    from personal_core_candidates import CoreCandidateQueue, CoreCandidateQueueError, exclusive_json_path_lock
    from personal_core_maintainer import (
        CoreMaintainerSettings,
        CoreMaintainerStateError,
        CoreMaintainerStateStore,
        CoreProfileMaintainer,
        MaintainerTrigger,
    )
    from personal_core_profile import (
        load_personal_core_profile_record,
        patch_personal_core_profile_sections,
        upgrade_personal_core_profile,
    )
    from personal_records import _require_write_mode
    from support import OperationCancelled, log_event


MAX_CORE_CANDIDATES_PER_JOB = 5


def bind_core_candidate(payload, entries, scope):
    """Keep only excerpts present in this timeline batch. Ignore model ids and foreign scope."""
    if not isinstance(payload, dict):
        return None
    if str(payload.get("op") or payload.get("action") or "").strip().lower() not in {"", "core_candidate"}:
        return None
    owner = str(scope or "").strip()
    for key in ("scope", "user_id", "agent_id"):
        foreign = payload.get(key)
        if foreign not in (None, "") and owner and str(foreign) != owner:
            return None
    user_excerpt = str(payload.get("user_excerpt") or "").strip()
    assistant_excerpt = str(payload.get("assistant_excerpt") or "").strip()
    if not user_excerpt or not assistant_excerpt:
        return None
    user_entry = next(
        (entry for entry in entries
         if getattr(entry, "role", "") in {"user", "observation"} and user_excerpt in str(entry.content or "")),
        None,
    )
    assistant_entry = next(
        (entry for entry in entries
         if getattr(entry, "role", "") == "assistant" and assistant_excerpt in str(entry.content or "")),
        None,
    )
    if user_entry is None or assistant_entry is None:
        return None
    batch_id = str(getattr(user_entry, "turn_id", "") or "").strip()
    if not batch_id:
        return None
    observed_at = str(getattr(user_entry, "created_at", "") or "").strip()
    if not observed_at:
        return None
    return {
        "kind": payload.get("kind"),
        "target_section": payload.get("target_section"),
        "subject_key": payload.get("subject_key"),
        "claim": payload.get("claim"),
        "user_excerpt": user_excerpt,
        "assistant_excerpt": assistant_excerpt,
        "confidence": payload.get("confidence"),
        "batch_id": batch_id,
        "observed_at": observed_at,
    }


class _CompletionAdapter:
    """One maintenance completion. Drop task/thinking and any result after cancel."""

    def __init__(self, client, cancel, closed):
        self._client = client
        self._cancel = cancel
        self._closed = closed
        self._used = False

    def complete_raw(self, system_prompt, messages, **kwargs):
        kwargs.pop("task", None)
        kwargs.pop("thinking", None)
        if self._used:
            raise RuntimeError("MAINTENANCE_REQUEST_LIMIT")
        self._used = True
        if self._cancel.is_set() or self._closed.is_set():
            raise OperationCancelled()

        def cancelled():
            if self._cancel.is_set() or self._closed.is_set():
                raise OperationCancelled()

        kwargs["cancel_checker"] = cancelled
        text = self._client.complete_raw(system_prompt, messages, **kwargs)
        cancelled()
        return text


class _ProfileStore:
    def __init__(self, memory_dir, scope, cancel, closed, *, daily=False):
        self._memory_dir = memory_dir
        self._scope = scope
        self._cancel = cancel
        self._closed = closed
        self._daily = daily

    def core_profile(self):
        return load_personal_core_profile_record(self._memory_dir, self._scope)

    def upgrade_legacy_core_profile(self):
        self._check_open()
        return upgrade_personal_core_profile(self._memory_dir, self._scope, daily=self._daily)

    def patch_core_profile_sections(self, base_updated_at, sections, candidate_ids=None, migrate_legacy=False):
        self._check_open()
        return patch_personal_core_profile_sections(
            self._memory_dir,
            self._scope,
            base_updated_at,
            sections,
            candidate_ids=candidate_ids,
            migrate_legacy=migrate_legacy,
            cancel_checker=self._check_open,
            daily=self._daily,
        )

    def _check_open(self):
        if self._cancel.is_set() or self._closed.is_set():
            raise OperationCancelled()


class ProfileMaintenance:
    def __init__(self, memory_dir, scope, *, settings=None, clock=None, cancel_event=None, daily=False):
        self._memory_dir = Path(memory_dir)
        self._scope = str(scope)
        self._daily = daily
        self._settings = _settings(settings)
        self._clock = clock or (lambda: __import__("datetime").datetime.now().astimezone())
        self._cancel = cancel_event or threading.Event()
        self._closed = threading.Event()
        self._queue = None
        self._state = None

    def enabled(self):
        return self._settings is not None and self._settings.enabled and not self._closed.is_set()

    def close(self):
        self._closed.set()
        self._cancel.set()
        if self._state is None:
            return
        # Wait for an already committing profile patch. A queued patch checks
        # cancellation again after acquiring this same lock. The runner's
        # finally releases only its own lease, including after a late response.
        with exclusive_json_path_lock(self._memory_dir / "core_profiles.json"):
            pass

    def persist_candidates(self, entries, payloads):
        if not self.enabled():
            return None
        _require_write_mode(self._memory_dir, self._scope, daily=self._daily,
                            write_rehearsal=not self._daily)
        grounded = []
        for payload in payloads or ():
            if len(grounded) >= MAX_CORE_CANDIDATES_PER_JOB:
                break
            bound = bind_core_candidate(payload, entries, self._scope)
            if bound is not None:
                grounded.append(bound)
        if not grounded:
            return None
        return _persist(self._queue_store(), self._scope, grounded)

    def run(self, trigger, completion):
        if not self.enabled():
            return None
        _require_write_mode(self._memory_dir, self._scope, daily=self._daily,
                            write_rehearsal=not self._daily)
        if self._closed.is_set():
            return None
        maintainer = CoreProfileMaintainer(
            api_client=_CompletionAdapter(completion, self._cancel, self._closed),
            memory_store=_ProfileStore(self._memory_dir, self._scope, self._cancel, self._closed,
                                       daily=self._daily),
            queue=self._queue_store(),
            state_store=self._state_store(),
            settings=self._settings,
            clock=self._clock,
        )
        return maintainer.run_once(self._scope, trigger)

    def _queue_store(self):
        if self._queue is None:
            self._queue = CoreCandidateQueue(
                self._memory_dir / "core_review_queue.json",
                clock=self._clock,
                config=self._settings.to_candidate_config(),
            )
        return self._queue

    def _state_store(self):
        if self._state is None:
            self._state = CoreMaintainerStateStore(
                self._memory_dir / "core_maintainer_state.json",
                clock=self._clock,
            )
        return self._state


def commit_curation_then_maintain(maintenance, entries, candidates, *, mark_success, completion):
    """Persist grounded candidates before the curation cursor, then maintain without undoing it."""
    if not maintenance.enabled():
        mark_success()
        return
    trigger = maintenance.persist_candidates(entries, candidates)
    mark_success()
    try:
        maintenance.run(trigger, completion)
    except (CoreCandidateQueueError, CoreMaintainerStateError, OSError, RuntimeError, ValueError) as exc:
        log_event(
            "Memory",
            "个人常驻档案维护失败",
            {"error_type": type(exc).__name__},
            event="memory.personal.core_profile_maintenance_failed",
            severity="warning",
        )


def _settings(value):
    if value is None:
        return None
    if isinstance(value, CoreMaintainerSettings):
        return value.normalized()
    if isinstance(value, dict):
        return CoreMaintainerSettings.from_mapping(value)
    return None


def _persist(queue, scope_id, payloads):
    ingested = []
    explicit_batch = ""
    for payload in payloads:
        candidate = queue.ingest(scope_id, payload)
        ingested.append(candidate)
        batch_id = str(payload.get("batch_id") or "").strip()
        if candidate.kind == "explicit" and batch_id:
            explicit_batch = batch_id
    eligible_ids = {item.id for item in queue.eligible_for(scope_id)}
    if explicit_batch:
        return MaintainerTrigger(kind="explicit", batch_id=explicit_batch)
    for candidate in ingested:
        if candidate.id in eligible_ids:
            return MaintainerTrigger(kind="observed", candidate_id=candidate.id)
    return None
